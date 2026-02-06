import os
import re
import logging
import sys
import pandas as pd
from flask import Flask, request, render_template, send_file, jsonify
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Load environment variables 
load_dotenv()

# --- Setup Logging for Railway ---
def setup_logging():
    """Configure logging for Railway deployment."""
    log_level = os.environ.get('LOG_LEVEL', 'INFO')
    log_file = os.environ.get('LOG_FILE', '/tmp/app.log')
    
    # Create log directory if it isn't there
    log_dir = os.path.dirname(log_file)
    os.makedirs(log_dir, exist_ok=True)
    
    # Configure logging to both file and stdout (Railway captures stdout)
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s:%(levelname)s:%(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )

setup_logging()

# --- Flask Application Setup ---
app = Flask(__name__)

# Railway-compatible configuration
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', '/tmp/uploads')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

# Create upload directory
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Chunk size for processing large files
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 5000))

# Define output file paths
model_order_list_path = os.path.join(app.config['UPLOAD_FOLDER'], 'model_order_list.csv')
exception_report_path = os.path.join(app.config['UPLOAD_FOLDER'], 'exception_report.csv')

logging.info(f"Application started with upload folder: {app.config['UPLOAD_FOLDER']}")
logging.info(f"Chunk size: {CHUNK_SIZE}")

# --- Helper Functions ---

def format_school_key(school_key):
    """Convert school_key to string and pad with zeros to 7 characters."""
    return str(school_key).strip().zfill(7)

def unwrap_school_key(school_key):
    """Remove Excel formula wrapping from school key."""
    if isinstance(school_key, str) and school_key.startswith('="') and school_key.endswith('"'):
        return school_key[2:-1]
    return str(school_key).strip()

def is_valid_email(email):
    """Return True if email matches basic pattern."""
    pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
    return re.match(pattern, str(email).strip()) is not None

def is_valid_uk_postcode(postcode):
    """Validate UK postcode format."""
    if pd.isna(postcode):
        return False
    pattern = r'^(GIR 0AA|[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2})$'
    return re.match(pattern, str(postcode).strip(), re.IGNORECASE) is not None

def row_invalid(row):
    """
    Returns True if any required field is missing or invalid.
    Checks: School key, School name, Email, Postcode, AH ISBN 1, AH SKU 1, AH Package 1
    """
    # Check Renewal Declined
    if pd.notna(row.get("Renewal Declined")) and str(row["Renewal Declined"]).strip().lower() == "yes":
        return True

    # Check required fields
    required_fields = ["School key", "School name", "Email", "Postcode", "AH ISBN 1", "AH SKU 1", "AH Package 1"]
    for field in required_fields:
        if pd.isna(row.get(field)) or str(row[field]).strip() == "":
            return True
    
    # Validate email and postcode
    if not is_valid_email(row["Email"]):
        return True
    #if not is_valid_uk_postcode(row["Postcode"]):
    #    return True
    
    return False

def validate_csv_file(file_path, file_description="file"):
    """
    Validate that a CSV file exists, is not empty, and has a valid structure.
    Returns: (success: bool, error_message: str or None, dataframe: pd.DataFrame or None)
    """
    import os

    # Check file exists
    if not os.path.exists(file_path):
        return False, f"{file_description} not found at {file_path}", None

    # Check file is not empty
    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return False, f"{file_description} is empty (0 bytes). Please upload a valid CSV file.", None

    # Try to read with multiple encodings
    encodings_to_try = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']
    last_error = None

    for encoding in encodings_to_try:
        try:
            # Try to read just the header and first few rows
            df = pd.read_csv(file_path, encoding=encoding, nrows=5)

            # Check if we got any columns
            if len(df.columns) == 0:
                return False, f"{file_description} has no columns. The file may not be a valid CSV format.", None

            # Check for unnamed columns (often indicates wrong delimiter)
            unnamed_cols = [col for col in df.columns if str(col).startswith('Unnamed:')]
            if len(unnamed_cols) == len(df.columns):
                return False, f"{file_description} appears to have no header row or uses an incorrect delimiter.", None

            logging.info(f"Successfully validated {file_description} with encoding '{encoding}'. Columns: {df.columns.tolist()[:5]}...")
            return True, None, df

        except pd.errors.EmptyDataError:
            return False, f"{file_description} contains no data. The file may be empty or contain only whitespace.", None
        except pd.errors.ParserError as e:
            last_error = f"{file_description} could not be parsed as CSV: {str(e)}"
            continue
        except UnicodeDecodeError:
            last_error = f"{file_description} encoding issue"
            continue
        except Exception as e:
            last_error = f"{file_description} error: {str(e)}"
            continue

    return False, f"{file_description} could not be read with any supported encoding (tried: {', '.join(encodings_to_try)}). Last error: {last_error}", None

def read_csv_with_fallback(file_path, file_description="file", **kwargs):
    """
    Read a CSV file, trying multiple encodings if needed.
    Returns the DataFrame or raises an exception with a helpful message.
    """
    encodings_to_try = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']
    last_error = None

    for encoding in encodings_to_try:
        try:
            df = pd.read_csv(file_path, encoding=encoding, **kwargs)
            logging.info(f"Successfully read {file_description} with encoding '{encoding}'")
            return df
        except (UnicodeDecodeError, pd.errors.ParserError) as e:
            last_error = str(e)
            continue

    raise ValueError(f"Could not read {file_description} with any supported encoding. Last error: {last_error}")

def apply_exclusions(model_order_list_df, exclude_csv_path):
    """
    Remove rows that match IDs in the Exclude.csv file.
    Returns: (valid_df, excluded_df)
    """
    if not os.path.exists(exclude_csv_path):
        return model_order_list_df, pd.DataFrame()
    
    try:
        exclude_df = pd.read_csv(exclude_csv_path, encoding='utf-8-sig')
    except Exception as e:
        logging.error(f"Error reading Exclude.csv: {e}")
        return model_order_list_df, pd.DataFrame()

    # Find the exclusion column
    possible_keys = ["Sub ID", "School key", "ISBN", "ALDS subscription product ISBN"]
    exclude_column = None
    for col in exclude_df.columns:
        if col.strip() in possible_keys:
            exclude_column = col.strip()
            break

    if not exclude_column:
        logging.warning("Exclude.csv missing recognized key column")
        return model_order_list_df, pd.DataFrame()

    if exclude_column not in model_order_list_df.columns:
        logging.warning(f"Model Order List missing column '{exclude_column}'")
        return model_order_list_df, pd.DataFrame()

    # Convert to string for comparison
    excluded_values = set(exclude_df[exclude_column].dropna().astype(str))
    is_excluded = model_order_list_df[exclude_column].astype(str).isin(excluded_values)

    return model_order_list_df[~is_excluded], model_order_list_df[is_excluded]

def group_orders_by_subid(df):
    """
    Groups orders by 'Sub ID' - each order becomes its own set of columns.
    Returns one row per Sub ID.
    """
    if "Sub ID" not in df.columns:
        raise ValueError("DataFrame missing 'Sub ID' column")

    # Log available columns for debugging
    logging.info(f"Grouping function - available columns: {df.columns.tolist()}")

    # Common columns (non-order specific) - these appear once per Sub ID
    common_cols = ["Customer name", "School key", "School name", "Email", "End date", "Address", "Postcode"]
    
    # Order-specific columns - these are pivoted with _1, _2, _3 suffixes
    possible_order_cols = [
        "ALDS subscription product name",
        "ALDS subscription product ISBN",
        "AH Line 1 Price",
        "AH ISBN 1",
        "AH SKU 1",
        "AH Package 1",
        "AH Sub Package Name",
        "AH SKU name",   # Standardized SKU name
        "AH Pack name"   # Package name
    ]
    sub_package_name_cols = sorted(
        [col for col in df.columns if re.match(r"^AH Sub \d+ Package Name$", col)]
    )
    possible_order_cols.extend(sub_package_name_cols)
    if "AH Sub Package Name" in df.columns:
        possible_order_cols.append("AH Sub Package Name")
    order_cols = [col for col in dict.fromkeys(possible_order_cols) if col in df.columns]
    
    # Log which columns were found
    logging.info(f"Order columns found for grouping: {order_cols}")
    not_found = [col for col in possible_order_cols if col not in df.columns]
    if not_found:
        logging.warning(f"Order columns NOT found in dataframe: {not_found}")

    # Assign order numbers within each Sub ID
    df_copy = df.copy()
    df_copy["order_num"] = df_copy.groupby("Sub ID").cumcount() + 1

    # Pivot order-specific fields
    pivot_df = df_copy.set_index(["Sub ID", "order_num"])[order_cols].unstack(fill_value="")
    
    # Flatten column names: e.g., ("AH Pack name", 1) becomes "AH Pack name_1"
    pivot_df.columns = [f"{col}_{num}" for col, num in pivot_df.columns]
    
    # Log sample of pivoted columns
    logging.info(f"Pivoted columns (first 10): {pivot_df.columns.tolist()[:10]}")

    # Aggregate common fields
    agg_funcs = {col: "first" for col in common_cols if col in df_copy.columns}
    common_df = df_copy.groupby("Sub ID").agg(agg_funcs)

    # Merge and return
    grouped_df = common_df.merge(pivot_df, left_index=True, right_index=True).reset_index()
    
    # Log final columns
    logging.info(f"Final grouped columns count: {len(grouped_df.columns)}")
    logging.info(f"Final grouped columns (first 20): {grouped_df.columns.tolist()[:20]}")
    
    return grouped_df

def compute_exception_report_stats(exception_csv_path):
    """Calculate statistics for exception report."""
    df = pd.read_csv(exception_csv_path, encoding='utf-8-sig')
    df["School key"] = df["School key"].apply(unwrap_school_key)
    
    df["is_uk"] = df["Postcode"].apply(lambda pc: is_valid_uk_postcode(str(pc)))
    
    return {
        "UK postcodes": df["is_uk"].sum(),
        "Non UK postcodes": len(df) - df["is_uk"].sum(),
        "Unique school keys": df["School key"].nunique(),
        "School keys starting with X": df["School key"].str.upper().str.startswith("X").sum()
    }

def test_als_isbn_consistency(raw_csv_path, mapping_csv_path, model_order_list_csv_path):
    """
    Tests ISBN consistency across all three files.
    Returns: (success: bool, message: str)
    """
    try:
        raw_df = pd.read_csv(raw_csv_path, encoding='utf-8-sig')
        mapping_df = pd.read_csv(mapping_csv_path, encoding='utf-8-sig')
        output_df = pd.read_csv(model_order_list_csv_path, encoding='utf-8-sig')
    except Exception as e:
        return False, f"Error reading CSV files: {e}"

    # Check required columns
    if "ISBN" not in raw_df.columns:
        return False, "Raw CSV missing 'ISBN' column"
    
    required_col = "ALDS subscription product ISBN"
    for name, df in [("Mapping", mapping_df), ("Output", output_df)]:
        if required_col not in df.columns:
            return False, f"{name} CSV missing '{required_col}' column"

    # Get common ISBNs
    raw_isbns = set(raw_df["ISBN"].dropna().astype(str))
    mapping_isbns = set(mapping_df[required_col].dropna().astype(str))
    output_isbns = set(output_df[required_col].dropna().astype(str))
    
    common_isbns = raw_isbns.intersection(mapping_isbns).intersection(output_isbns)

    if common_isbns:
        return True, f"Test passed. {len(common_isbns)} common ISBN(s) found"
    else:
        return False, "No common ALDS subscription product ISBN found in all files"

def run_all_tests(mapping_csv_path, model_order_list_csv_path):
    """
    Run comprehensive validation tests on the processed order list.
    Returns: list of test results, each with {name, passed, message, details}
    """
    results = []

    # Load the files
    try:
        mapping_df = pd.read_csv(mapping_csv_path, encoding='utf-8-sig')
        order_df = pd.read_csv(model_order_list_csv_path, encoding='utf-8-sig')
    except Exception as e:
        return [{
            "name": "File Loading",
            "passed": False,
            "message": f"Could not load files for testing: {str(e)}",
            "details": ["Ensure files have been processed before running tests"]
        }]

    if len(order_df) == 0:
        return [{
            "name": "File Loading",
            "passed": False,
            "message": "Order list is empty - no data to test",
            "details": ["Process files first to generate the order list"]
        }]

    # === TEST 1: ActiveHub Package Check ===
    # Check Pack ID in order list matches Pack ID in mapping CSV
    test1_details = []
    test1_passed = True
    test1_mismatches = 0

    try:
        # Check if required columns exist
        order_pack_col = "AH Package 1" if "AH Package 1" in order_df.columns else None
        mapping_pack_col = "AH Sub 1 Package" if "AH Sub 1 Package" in mapping_df.columns else None
        isbn_col = "ALDS subscription product ISBN"

        if not order_pack_col:
            test1_passed = False
            test1_details.append("Order list missing 'AH Package 1' column")
        elif not mapping_pack_col:
            test1_passed = False
            test1_details.append("Mapping file missing 'AH Sub 1 Package' column")
        elif isbn_col not in order_df.columns or isbn_col not in mapping_df.columns:
            test1_passed = False
            test1_details.append(f"Missing '{isbn_col}' column for matching")
        else:
            # Create mapping lookup
            mapping_lookup = mapping_df.set_index(isbn_col)[mapping_pack_col].to_dict()

            for idx, row in order_df.iterrows():
                order_isbn = str(row.get(isbn_col, "")).strip()
                order_pack = str(row.get(order_pack_col, "")).strip()
                expected_pack = str(mapping_lookup.get(order_isbn, "")).strip()

                if order_isbn and expected_pack and order_pack != expected_pack:
                    test1_mismatches += 1
                    if test1_mismatches <= 5:  # Show first 5 mismatches
                        test1_details.append(f"Row {idx+2}: ISBN {order_isbn} has Pack '{order_pack}' but expected '{expected_pack}'")

            if test1_mismatches > 0:
                test1_passed = False
                test1_details.insert(0, f"Found {test1_mismatches} Pack ID mismatches")
                if test1_mismatches > 5:
                    test1_details.append(f"...and {test1_mismatches - 5} more mismatches")
            else:
                test1_details.append(f"All {len(order_df)} orders have correct Pack IDs")
    except Exception as e:
        test1_passed = False
        test1_details.append(f"Error during test: {str(e)}")

    results.append({
        "name": "ActiveHub Package Check",
        "passed": test1_passed,
        "message": "Pack IDs match mapping data" if test1_passed else "Pack ID mismatches found",
        "details": test1_details
    })

    # === TEST 2: ActiveHub Subject/SKU Check ===
    # Check SKU name and number match between order list and mapping
    test2_details = []
    test2_passed = True
    test2_mismatches = 0

    try:
        isbn_col = "ALDS subscription product ISBN"
        # Check for SKU columns (handle different capitalizations)
        order_sku_col = None
        mapping_sku_col = None

        for col in ["AH SKU 1", "AH Sku 1"]:
            if col in order_df.columns:
                order_sku_col = col
                break

        for col in ["AH Sub 1 SKU"]:
            if col in mapping_df.columns:
                mapping_sku_col = col
                break

        # SKU name columns
        order_sku_name_col = None
        mapping_sku_name_col = None

        for col in ["AH SKU name", "AH SKU 1 name"]:
            if col in order_df.columns:
                order_sku_name_col = col
                break

        for col in ["AH SKU name", "AH Sku name", "AH SKU 1 name"]:
            if col in mapping_df.columns:
                mapping_sku_name_col = col
                break

        if not order_sku_col:
            test2_passed = False
            test2_details.append("Order list missing SKU ID column (AH SKU 1)")
        elif not mapping_sku_col:
            test2_passed = False
            test2_details.append("Mapping file missing SKU column (AH Sub 1 SKU)")
        else:
            # Create mapping lookups
            mapping_sku_lookup = mapping_df.set_index(isbn_col)[mapping_sku_col].to_dict()
            mapping_sku_name_lookup = {}
            if mapping_sku_name_col:
                mapping_sku_name_lookup = mapping_df.set_index(isbn_col)[mapping_sku_name_col].to_dict()

            for idx, row in order_df.iterrows():
                order_isbn = str(row.get(isbn_col, "")).strip()
                order_sku = str(row.get(order_sku_col, "")).strip()
                expected_sku = str(mapping_sku_lookup.get(order_isbn, "")).strip()

                if order_isbn and expected_sku and order_sku != expected_sku:
                    test2_mismatches += 1
                    if test2_mismatches <= 5:
                        test2_details.append(f"Row {idx+2}: ISBN {order_isbn} has SKU '{order_sku}' but expected '{expected_sku}'")

                # Also check SKU name if available
                if order_sku_name_col and mapping_sku_name_col:
                    order_sku_name = str(row.get(order_sku_name_col, "")).strip()
                    expected_sku_name = str(mapping_sku_name_lookup.get(order_isbn, "")).strip()
                    if order_isbn and expected_sku_name and order_sku_name != expected_sku_name:
                        test2_mismatches += 1
                        if test2_mismatches <= 5:
                            test2_details.append(f"Row {idx+2}: SKU name mismatch - got '{order_sku_name}', expected '{expected_sku_name}'")

            if test2_mismatches > 0:
                test2_passed = False
                test2_details.insert(0, f"Found {test2_mismatches} SKU mismatches")
                if test2_mismatches > 5:
                    test2_details.append(f"...and {test2_mismatches - 5} more mismatches")
            else:
                test2_details.append(f"All {len(order_df)} orders have correct SKU IDs and names")
    except Exception as e:
        test2_passed = False
        test2_details.append(f"Error during test: {str(e)}")

    results.append({
        "name": "ActiveHub Subject/SKU Check",
        "passed": test2_passed,
        "message": "SKU IDs and names match mapping data" if test2_passed else "SKU mismatches found",
        "details": test2_details
    })

    # === TEST 3: Pack ID and SKU ID Populated Check ===
    # Ensure SKU codes are populated in the order list
    test3_details = []
    test3_passed = True
    missing_pack = 0
    missing_sku = 0

    try:
        pack_col = "AH Package 1" if "AH Package 1" in order_df.columns else None
        sku_col = None
        for col in ["AH SKU 1", "AH Sku 1"]:
            if col in order_df.columns:
                sku_col = col
                break

        if not pack_col:
            test3_details.append("Warning: 'AH Package 1' column not found")
        if not sku_col:
            test3_details.append("Warning: 'AH SKU 1' column not found")

        if pack_col:
            missing_pack = order_df[pack_col].isna().sum() + (order_df[pack_col].astype(str).str.strip() == "").sum()
        if sku_col:
            missing_sku = order_df[sku_col].isna().sum() + (order_df[sku_col].astype(str).str.strip() == "").sum()

        if missing_pack > 0:
            test3_passed = False
            test3_details.append(f"{missing_pack} orders have empty Pack ID")
        else:
            test3_details.append(f"All {len(order_df)} orders have Pack ID populated")

        if missing_sku > 0:
            test3_passed = False
            test3_details.append(f"{missing_sku} orders have empty SKU ID")
        else:
            test3_details.append(f"All {len(order_df)} orders have SKU ID populated")

    except Exception as e:
        test3_passed = False
        test3_details.append(f"Error during test: {str(e)}")

    results.append({
        "name": "Pack ID and SKU ID Populated",
        "passed": test3_passed,
        "message": "All required codes are populated" if test3_passed else "Some orders missing Pack ID or SKU ID",
        "details": test3_details
    })

    # === TEST 4: Email Validation (ends with 1 or 2) ===
    # Emails ending with 1 or 2 will fail
    test4_details = []
    test4_passed = True
    problem_emails = []

    try:
        if "Email" not in order_df.columns:
            test4_passed = False
            test4_details.append("Email column not found in order list")
        else:
            for idx, row in order_df.iterrows():
                email = str(row.get("Email", "")).strip()
                # Check if email ends with 1 or 2 (before @domain or at the very end of local part)
                if email:
                    local_part = email.split("@")[0] if "@" in email else email
                    if local_part.endswith("1") or local_part.endswith("2"):
                        problem_emails.append((idx + 2, email))

            if problem_emails:
                test4_passed = False
                test4_details.append(f"Found {len(problem_emails)} emails ending with 1 or 2 (these orders will fail)")
                for row_num, email in problem_emails[:5]:
                    test4_details.append(f"Row {row_num}: {email}")
                if len(problem_emails) > 5:
                    test4_details.append(f"...and {len(problem_emails) - 5} more problem emails")
            else:
                test4_details.append(f"All {len(order_df)} emails passed validation")
    except Exception as e:
        test4_passed = False
        test4_details.append(f"Error during test: {str(e)}")

    results.append({
        "name": "Email Validation",
        "passed": test4_passed,
        "message": "All emails are valid" if test4_passed else f"{len(problem_emails)} emails will cause order failures",
        "details": test4_details
    })

    # === TEST 5: Price Consistency Check ===
    # Check order list price matches mapping list price
    test5_details = []
    test5_passed = True
    price_mismatches = 0

    try:
        isbn_col = "ALDS subscription product ISBN"
        order_price_col = "AH Line 1 Price" if "AH Line 1 Price" in order_df.columns else None
        mapping_price_col = "ISBN ExVAT List Price" if "ISBN ExVAT List Price" in mapping_df.columns else None

        if not order_price_col:
            test5_passed = False
            test5_details.append("Order list missing 'AH Line 1 Price' column")
        elif not mapping_price_col:
            test5_passed = False
            test5_details.append("Mapping file missing 'ISBN ExVAT List Price' column")
        elif isbn_col not in mapping_df.columns:
            test5_passed = False
            test5_details.append(f"Mapping file missing '{isbn_col}' column")
        else:
            # Create price lookup
            mapping_price_lookup = mapping_df.set_index(isbn_col)[mapping_price_col].to_dict()

            for idx, row in order_df.iterrows():
                order_isbn = str(row.get(isbn_col, "")).strip()

                try:
                    order_price = float(row.get(order_price_col, 0)) if pd.notna(row.get(order_price_col)) else None
                    expected_price = float(mapping_price_lookup.get(order_isbn, 0)) if order_isbn in mapping_price_lookup else None

                    if order_price is not None and expected_price is not None:
                        # Compare with tolerance for floating point
                        if abs(order_price - expected_price) > 0.01:
                            price_mismatches += 1
                            if price_mismatches <= 5:
                                test5_details.append(f"Row {idx+2}: ISBN {order_isbn} has price {order_price:.2f} but expected {expected_price:.2f}")
                except (ValueError, TypeError):
                    pass  # Skip rows with non-numeric prices

            if price_mismatches > 0:
                test5_passed = False
                test5_details.insert(0, f"Found {price_mismatches} price mismatches")
                if price_mismatches > 5:
                    test5_details.append(f"...and {price_mismatches - 5} more mismatches")
            else:
                test5_details.append(f"All {len(order_df)} orders have correct prices")
    except Exception as e:
        test5_passed = False
        test5_details.append(f"Error during test: {str(e)}")

    results.append({
        "name": "Price Consistency Check",
        "passed": test5_passed,
        "message": "All prices match mapping data" if test5_passed else "Price mismatches found",
        "details": test5_details
    })

    return results

def process_chunk(chunk_df, mapping_df, chunk_num):
    """
    Process a single chunk of raw data.
    Returns: (valid_orders_df, exception_orders_df)
    """
    logging.info(f"Processing chunk {chunk_num} with {len(chunk_df)} rows")

    def find_column(df, candidate_names):
        """Return first matching column from candidate names (case/space-insensitive)."""
        normalized = {str(col).strip().lower(): col for col in df.columns}
        for name in candidate_names:
            match = normalized.get(str(name).strip().lower())
            if match is not None:
                return match
        return None
    
    # Normalize ISBN format - convert to consistent string format
    # Handle both integer and float ISBNs, and deal with NaN values
    def normalize_isbn(isbn):
        try:
            if pd.isna(isbn):
                return ""
            # Convert to float first (handles both int and float), then to int, then to string
            return str(int(float(isbn)))
        except (ValueError, TypeError):
            return str(isbn).strip()
    
    chunk_df['ISBN'] = chunk_df['ISBN'].apply(normalize_isbn)
    
    # Log sample ISBNs for debugging
    if chunk_num == 1:
        logging.info(f"Sample raw ISBNs: {chunk_df['ISBN'].head(3).tolist()}")
        logging.info(f"Sample mapping ISBNs: {mapping_df['ALDS subscription product ISBN'].head(3).tolist()}")
        logging.info(f"Raw data columns: {chunk_df.columns.tolist()}")
        logging.info(f"Checking for 'Sub ID' column: {'Sub ID' in chunk_df.columns}")
        logging.info(f"Mapping columns available: {mapping_df.columns.tolist()}")
    
    # Perform merge
    merged_df = pd.merge(
        chunk_df,
        mapping_df,
        left_on='ISBN',
        right_on='ALDS subscription product ISBN',
        how='left'
    )
    
    # Log merge success rate
    successful_merges = merged_df['ALDS subscription product ISBN'].notna().sum()
    logging.info(f"Chunk {chunk_num}: {successful_merges}/{len(chunk_df)} rows matched to mapping data")
    
    # Verify Sub ID column exists after merge
    if chunk_num == 1 and 'Sub ID' not in merged_df.columns:
        logging.warning("WARNING: 'Sub ID' column is missing from merged data!")
    
    # Build Model Order List efficiently
    model_order_list = pd.DataFrame()
    
    # CRITICAL: Explicitly add Sub ID first if it exists
    if 'Sub ID' in merged_df.columns:
        model_order_list["Sub ID"] = merged_df["Sub ID"]
    else:
        logging.warning(f"Chunk {chunk_num}: 'Sub ID' column missing from raw data - grouping will not be available")
    
    def add_sub_package_name_columns():
        pattern = re.compile(r"^ah sub (\d+) package name(_y)?$", re.IGNORECASE)
        normalized_map = {}
        for col in merged_df.columns:
            match = pattern.match(str(col).strip())
            if not match:
                continue
            number = match.group(1)
            normalized_col = f"AH Sub {number} Package Name"
            is_merge_suffix = match.group(2) is not None
            if normalized_col not in normalized_map or not is_merge_suffix:
                normalized_map[normalized_col] = col
        for normalized_col, source_col in normalized_map.items():
            model_order_list[normalized_col] = merged_df[source_col]

    # Define other columns to map
    columns_map = {
        "Customer name": ["Customer name"],
        "School name": ["School name"],
        "Email": ["Email"],
        "End date": ["End date"],
        "Address": ["Address"],
        "Postcode": ["Postcode"],
        "ALDS subscription product name": ["ALDS subscription product name"],
        "ALDS subscription product ISBN": ["ALDS subscription product ISBN"],
        "Renewal Declined": ["Renewal Declined"],
        "AH Sub Package Name": [
            "AH Sub Package Name",
            "AH Sub 1 Sub Package Name",
            "AH Sub 1 Package Name",
            "AH Sub Package Name_y",
            "AH Sub 1 Package Name_y",
            "AH Sub 1 Sub Package Name_y"
        ],
        "AH SKU name": ["AH SKU 1 name", "AH SKU name", "AH Sku name"],
        "AH Pack name": ["AH Pack name"]

    }

    # Select columns efficiently
    for new_col, source_candidates in columns_map.items():
        source_col = find_column(merged_df, source_candidates)
        if source_col is not None:
            model_order_list[new_col] = merged_df[source_col]

    add_sub_package_name_columns()
    
    # Log which mapping columns were found
    if chunk_num == 1:
        sub_package_name_cols = sorted(
            [col for col in model_order_list.columns if re.match(r"^AH Sub \d+ Package Name$", col)]
        )
        debug_col_aliases = {
            "AH Sub Package Name": [
                "AH Sub Package Name",
                "AH Sub 1 Sub Package Name",
                "AH Sub 1 Package Name",
                "AH Sub Package Name_y",
                "AH Sub 1 Package Name_y",
                "AH Sub 1 Sub Package Name_y"
            ],
            "AH SKU name": ["AH SKU 1 name", "AH SKU name", "AH Sku name"],
            "AH Pack name": ["AH Pack name"]
        }
        found_cols = [
            col for col in debug_col_aliases
            if col in model_order_list.columns
        ]
        missing_cols = [
            col for col, aliases in debug_col_aliases.items()
            if find_column(merged_df, aliases) is None
        ]
        logging.info(f"Mapping columns found in output: {found_cols}")
        if sub_package_name_cols:
            logging.info(f"Mapping sub package name columns found: {sub_package_name_cols}")
        if missing_cols:
            logging.warning(f"Mapping columns NOT found in merged data: {missing_cols}")
    
    # Format school key
    model_order_list["School key"] = merged_df["School key"].apply(format_school_key)

    # Ensure School key is adjacent to School name in output
    if "School key" in model_order_list.columns and "School name" in model_order_list.columns:
        reordered_cols = [col for col in model_order_list.columns if col != "School key"]
        school_name_index = reordered_cols.index("School name")
        reordered_cols.insert(school_name_index + 1, "School key")
        model_order_list = model_order_list[reordered_cols]
    
    # Add pricing and product info
    model_order_list["AH Line 1 Price"] = merged_df["ISBN ExVAT List Price"]
    model_order_list["AH ISBN 1"] = merged_df["AH Sub 1 ISBN"]
    model_order_list["AH SKU 1"] = merged_df["AH Sub 1 SKU"]
    model_order_list["AH Package 1"] = merged_df["AH Sub 1 Package"]
    if "AH SKU 1 name" in merged_df.columns and "AH SKU name" not in model_order_list.columns:
        model_order_list["AH SKU name"] = merged_df["AH SKU 1 name"]

    # Validate rows
    invalid_mask = model_order_list.apply(row_invalid, axis=1)
    
    valid_orders = model_order_list[~invalid_mask].copy()
    exception_orders = model_order_list[invalid_mask].copy()
    
    # Log chunk results including Sub ID status
    logging.info(f"Chunk {chunk_num}: {len(valid_orders)} valid, {len(exception_orders)} exceptions")
    logging.info(f"Chunk {chunk_num}: Valid orders columns: {valid_orders.columns.tolist()}")
    logging.info(f"Chunk {chunk_num}: 'Sub ID' in valid orders: {'Sub ID' in valid_orders.columns}")
    
    return valid_orders, exception_orders

def process_files_chunked(raw_file_path, mapping_df, exclude_csv_path=None):
    """
    Process raw data file in chunks to handle large datasets.
    Returns: (valid_df, exception_df, exclusions_applied)
    """
    try:
        # Check available disk space (Railway provides limited space)
        import shutil
        try:
            total, used, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
            free_gb = free / (1024**3)
            logging.info(f"Available disk space: {free_gb:.2f}GB")
            if free_gb < 0.5:  # Less than 500MB free
                logging.warning(f"Low disk space warning: {free_gb:.2f}GB available")
        except:
            logging.warning("Could not check disk space")

        # Normalize mapping ISBN format - convert to consistent string format
        # Handle float ISBNs and NaN values
        def normalize_isbn(isbn):
            try:
                if pd.isna(isbn):
                    return ""
                # Convert to float first (handles both int and float), then to int, then to string
                return str(int(float(isbn)))
            except (ValueError, TypeError):
                return str(isbn).strip()
        
        mapping_df['ALDS subscription product ISBN'] = mapping_df['ALDS subscription product ISBN'].apply(normalize_isbn)

        # Normalize mapping headers so columns with leading/trailing spaces still map correctly
        mapping_df.columns = [str(col).strip() for col in mapping_df.columns]
        
        # Get total rows for progress tracking
        total_rows = sum(1 for _ in open(raw_file_path, encoding='latin1')) - 1  # Subtract header
        logging.info(f"Processing {total_rows} total rows in chunks of {CHUNK_SIZE}")

        if total_rows <= 0:
            raise ValueError("Raw Data file contains no data rows. Please upload a file with at least one data row.")

        # Initialize result lists
        valid_chunks = []
        exception_chunks = []

        chunk_num = 0

        # Try multiple encodings for reading raw file
        encodings_to_try = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']
        successful_encoding = None
        last_error = None

        for encoding in encodings_to_try:
            try:
                # Test reading first chunk with this encoding
                test_reader = pd.read_csv(raw_file_path, encoding=encoding, chunksize=CHUNK_SIZE, low_memory=False)
                test_chunk = next(test_reader)
                if len(test_chunk.columns) == 0:
                    continue
                successful_encoding = encoding
                logging.info(f"Successfully reading Raw Data file with encoding '{encoding}'")
                break
            except (UnicodeDecodeError, pd.errors.ParserError, StopIteration) as e:
                last_error = str(e)
                continue

        if successful_encoding is None:
            raise ValueError(f"Could not read Raw Data file with any supported encoding (tried: {', '.join(encodings_to_try)}). Last error: {last_error}")

        # Process file in chunks with the successful encoding
        for chunk_df in pd.read_csv(raw_file_path, encoding=successful_encoding, chunksize=CHUNK_SIZE, low_memory=False):
            chunk_num += 1
            
            # Process this chunk
            valid_chunk, exception_chunk = process_chunk(chunk_df, mapping_df, chunk_num)
            
            # Accumulate results
            if len(valid_chunk) > 0:
                valid_chunks.append(valid_chunk)
            if len(exception_chunk) > 0:
                exception_chunks.append(exception_chunk)
            
            # Log progress
            processed_rows = min(chunk_num * CHUNK_SIZE, total_rows)
            progress = (processed_rows / total_rows) * 100
            logging.info(f"Progress: {processed_rows}/{total_rows} ({progress:.1f}%)")
        
        # Combine all chunks
        logging.info("Combining all processed chunks...")
        valid_orders = pd.concat(valid_chunks, ignore_index=True) if valid_chunks else pd.DataFrame()
        exception_orders = pd.concat(exception_chunks, ignore_index=True) if exception_chunks else pd.DataFrame()
        
        logging.info(f"Total after processing: {len(valid_orders)} valid, {len(exception_orders)} exceptions")
        logging.info(f"Columns after concatenation: {valid_orders.columns.tolist()}")
        logging.info(f"'Sub ID' after concatenation: {'Sub ID' in valid_orders.columns}")

        # Apply exclusions if provided
        exclusions_applied = False
        if exclude_csv_path and os.path.exists(exclude_csv_path):
            logging.info("Applying exclusions...")
            valid_orders, excluded_rows = apply_exclusions(valid_orders, exclude_csv_path)
            if len(excluded_rows) > 0:
                exception_orders = pd.concat([exception_orders, excluded_rows], ignore_index=True)
            exclusions_applied = True
            logging.info(f"After exclusions: {len(valid_orders)} valid, {len(excluded_rows)} excluded")

        return valid_orders, exception_orders, exclusions_applied

    except Exception as e:
        logging.error(f"Chunked processing error: {str(e)}")
        raise

# --- Routes ---

@app.route("/health")
def health_check():
    """Health check endpoint for Railway."""
    return jsonify({
        "status": "healthy",
        "upload_folder": app.config['UPLOAD_FOLDER'],
        "chunk_size": CHUNK_SIZE,
        "environment": os.environ.get('FLASK_ENV', 'development')
    })

@app.route("/", methods=["GET", "POST"])
def upload_files():
    if request.method == "POST":
        try:
            # Validate file uploads exist in request
            if "raw_data" not in request.files or "mapping_data" not in request.files:
                return "Missing file upload. Please select both Raw Data and Mapping Data files.", 400

            raw_file = request.files["raw_data"]
            mapping_file = request.files["mapping_data"]
            exclude_file = request.files.get("exclude_data")

            if not raw_file.filename or not mapping_file.filename:
                return "No file selected. Please select both Raw Data and Mapping Data files.", 400

            # Check file extensions
            if not raw_file.filename.lower().endswith('.csv'):
                return f"Raw Data file must be a CSV file. Received: {raw_file.filename}", 400
            if not mapping_file.filename.lower().endswith('.csv'):
                return f"Mapping Data file must be a CSV file. Received: {mapping_file.filename}", 400

            # Save uploaded files
            raw_file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename("raw_data.csv"))
            mapping_file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename("example_mapping.csv"))
            exclude_csv_path = None

            raw_file.save(raw_file_path)
            mapping_file.save(mapping_file_path)

            if exclude_file and exclude_file.filename:
                if not exclude_file.filename.lower().endswith('.csv'):
                    return f"Exclude file must be a CSV file. Received: {exclude_file.filename}", 400
                exclude_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename("Exclude.csv"))
                exclude_file.save(exclude_csv_path)

            # Validate Raw Data file
            logging.info("Validating Raw Data file...")
            valid, error_msg, _ = validate_csv_file(raw_file_path, "Raw Data file")
            if not valid:
                logging.error(f"Raw Data validation failed: {error_msg}")
                return f"Raw Data file error: {error_msg}", 400

            # Validate Mapping file
            logging.info("Validating Mapping file...")
            valid, error_msg, _ = validate_csv_file(mapping_file_path, "Mapping file")
            if not valid:
                logging.error(f"Mapping file validation failed: {error_msg}")
                return f"Mapping file error: {error_msg}", 400

            # Validate Exclude file if provided
            if exclude_csv_path:
                logging.info("Validating Exclude file...")
                valid, error_msg, _ = validate_csv_file(exclude_csv_path, "Exclude file")
                if not valid:
                    logging.error(f"Exclude file validation failed: {error_msg}")
                    return f"Exclude file error: {error_msg}", 400

            # Read mapping file with encoding fallback
            logging.info("Reading mapping file...")
            try:
                mapping_df = read_csv_with_fallback(mapping_file_path, "Mapping file", low_memory=False)
            except ValueError as e:
                return str(e), 400

            # Validate mapping file has required columns
            required_mapping_cols = ["ALDS subscription product ISBN"]
            missing_cols = [col for col in required_mapping_cols if col not in mapping_df.columns]
            if missing_cols:
                return f"Mapping file is missing required columns: {', '.join(missing_cols)}. Found columns: {', '.join(mapping_df.columns.tolist()[:10])}...", 400

            logging.info(f"Loaded {len(mapping_df)} mapping records")

            # Process raw file in chunks
            logging.info("Starting chunked processing...")
            valid_orders, exception_orders, exclusions_applied = process_files_chunked(
                raw_file_path, mapping_df, exclude_csv_path
            )

            # Save results
            logging.info("Saving results to CSV...")
            logging.info(f"Columns in valid_orders before saving: {valid_orders.columns.tolist()}")
            logging.info(f"'Sub ID' in valid_orders: {'Sub ID' in valid_orders.columns}")

            # Save with utf-8-sig to preserve proper encoding and prevent BOM issues
            valid_orders.to_csv(model_order_list_path, index=False, encoding='utf-8-sig')
            exception_orders.to_csv(exception_report_path, index=False, encoding='utf-8-sig')

            # Verify what was actually saved (only if we have valid orders)
            if len(valid_orders) > 0:
                saved_df = pd.read_csv(model_order_list_path, encoding='utf-8-sig', nrows=1)
                logging.info(f"Columns in saved CSV: {saved_df.columns.tolist()}")
                logging.info(f"'Sub ID' in saved CSV: {'Sub ID' in saved_df.columns}")
            else:
                logging.warning("No valid orders to save - all records were exceptions")

            logging.info("Processing complete!")

            # Prepare for display (limit preview to first 1000 rows for performance)
            preview_limit = 1000
            valid_preview = valid_orders.head(preview_limit).to_csv(index=False)
            exception_preview = exception_orders.head(preview_limit).to_csv(index=False)

            valid_stats = {
                "valid_orders": len(valid_orders),
                "exceptions": len(exception_orders),
                "excluded": 0,  # Count calculated during exclusion phase
                "exclusions_applied": exclusions_applied,
                "preview_limit": preview_limit if len(valid_orders) > preview_limit else None
            }

            return render_template("results.html",
                                   valid_csv=valid_preview,
                                   exception_csv=exception_preview,
                                   valid_count=len(valid_orders),
                                   exception_count=len(exception_orders),
                                   valid_stats=valid_stats)

        except pd.errors.EmptyDataError as e:
            logging.error(f"Empty data error: {str(e)}")
            return "Processing error: One of the uploaded files contains no data. Please check that your CSV files are not empty.", 500
        except pd.errors.ParserError as e:
            logging.error(f"Parser error: {str(e)}")
            return f"Processing error: Could not parse CSV file. The file may be corrupted or use an unsupported format. Details: {str(e)}", 500
        except KeyError as e:
            logging.error(f"Missing column error: {str(e)}")
            return f"Processing error: Required column not found in data: {str(e)}. Please check that your files have the correct column headers.", 500
        except Exception as e:
            logging.error(f"Upload error: {str(e)}")
            return f"Processing error: {e}", 500

    return render_template("upload.html")

@app.route("/download_valid")
def download_valid():
    """Download the valid Model Order List CSV."""
    if not os.path.exists(model_order_list_path):
        return "Model Order List not found. Please process files first.", 404
    return send_file(model_order_list_path,
                     as_attachment=True,
                     download_name="model_order_list.csv",
                     mimetype="text/csv")

@app.route("/download_exception")
def download_exception():
    """Download the Exception Report CSV."""
    if not os.path.exists(exception_report_path):
        return "Exception Report not found. Please process files first.", 404
    return send_file(exception_report_path,
                     as_attachment=True,
                     download_name="exception_report.csv",
                     mimetype="text/csv")

@app.route("/download_grouped")
def download_grouped():
    """Download orders grouped by Sub ID (processes in chunks for memory efficiency)."""
    try:
        # Check if model order list exists
        if not os.path.exists(model_order_list_path):
            return "Model Order List not found. Please process files first.", 404
        
        # For grouping, we need to load the full file
        # Use utf-8-sig to handle BOM properly
        logging.info("Reading model order list for grouping...")
        df = pd.read_csv(model_order_list_path, encoding='utf-8-sig', low_memory=False)
        
        # Log what columns are available before grouping
        logging.info(f"Columns in Model Order List before grouping: {df.columns.tolist()}")
        logging.info(f"Checking for name columns: AH SKU name={('AH SKU name' in df.columns)}, AH Pack name={('AH Pack name' in df.columns)}")
        
        # Check if Sub ID column exists
        if 'Sub ID' not in df.columns:
            logging.error("Sub ID column missing from Model Order List")
            available_cols = df.columns.tolist()
            logging.error(f"Available columns: {available_cols}")
            return f"Cannot group orders: 'Sub ID' column is missing from the processed data. This may indicate the raw data file doesn't contain Sub IDs. Available columns: {', '.join(available_cols[:10])}...", 400
        
        logging.info(f"Grouping {len(df)} orders by Sub ID...")
        grouped_df = group_orders_by_subid(df)
        
        # Log what columns are in the grouped output
        logging.info(f"Columns in grouped output: {grouped_df.columns.tolist()}")
        
        grouped_path = os.path.join(app.config['UPLOAD_FOLDER'], 'grouped_model_order_list.csv')
        grouped_df.to_csv(grouped_path, index=False, encoding='utf-8-sig')
        
        logging.info("Grouping complete")
        
        return send_file(grouped_path,
                         as_attachment=True,
                         download_name="grouped_model_order_list.csv",
                         mimetype="text/csv")
    except Exception as e:
        logging.error(f"Grouping error: {str(e)}")
        return f"Error grouping orders: {str(e)}", 500

@app.route("/test_als_isbn")
def test_als_isbn_ui():
    """Run comprehensive validation tests on processed data."""
    raw_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], "raw_data.csv")
    mapping_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], "example_mapping.csv")

    # Check if files exist
    if not os.path.exists(model_order_list_path):
        return render_template("test_results.html",
                               test_results=[],
                               overall_passed=False,
                               error_message="No processed data found. Please upload and process file first.")

    if not os.path.exists(mapping_csv_path):
        return render_template("test_results.html",
                               test_results=[],
                               overall_passed=False,
                               error_message="Mapping file not found. Please upload files first.")

    # Run ISBN consistency test (legacy)
    isbn_result, isbn_message = test_als_isbn_consistency(raw_csv_path, mapping_csv_path, model_order_list_path)

    # Run comprehensive tests
    test_results = run_all_tests(mapping_csv_path, model_order_list_path)

    # Add ISBN test as first test
    test_results.insert(0, {
        "name": "ISBN Consistency Check",
        "passed": isbn_result,
        "message": isbn_message,
        "details": ["Verifies ISBNs match across Raw Data, Mapping, and Output files"]
    })

    # Calculate overall result
    overall_passed = all(test["passed"] for test in test_results)
    passed_count = sum(1 for test in test_results if test["passed"])
    failed_count = len(test_results) - passed_count

    return render_template("test_results.html",
                           test_results=test_results,
                           overall_passed=overall_passed,
                           passed_count=passed_count,
                           failed_count=failed_count,
                           error_message=None)

@app.route("/debug_columns")
def debug_columns():
    """Debug endpoint to show what columns are in saved files."""
    try:
        info = {}
        
        # Check model order list
        if os.path.exists(model_order_list_path):
            df = pd.read_csv(model_order_list_path, encoding='utf-8-sig', nrows=5)
            info['model_order_list'] = {
                'exists': True,
                'columns': df.columns.tolist(),
                'has_sub_id': 'Sub ID' in df.columns,
                'row_count': len(pd.read_csv(model_order_list_path, encoding='utf-8-sig'))
            }
        else:
            info['model_order_list'] = {'exists': False}
        
        # Check raw data
        raw_path = os.path.join(app.config['UPLOAD_FOLDER'], "raw_data.csv")
        if os.path.exists(raw_path):
            df = pd.read_csv(raw_path, encoding='utf-8-sig', nrows=5)
            info['raw_data'] = {
                'exists': True,
                'columns': df.columns.tolist(),
                'has_sub_id': 'Sub ID' in df.columns
            }
        else:
            info['raw_data'] = {'exists': False}
        
        # Check mapping
        mapping_path = os.path.join(app.config['UPLOAD_FOLDER'], "example_mapping.csv")
        if os.path.exists(mapping_path):
            df = pd.read_csv(mapping_path, encoding='utf-8-sig', nrows=5)
            info['mapping'] = {
                'exists': True,
                'columns': df.columns.tolist()
            }
        else:
            info['mapping'] = {'exists': False}
        
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug)
