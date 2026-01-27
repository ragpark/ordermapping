import os
import re
import logging
import pandas as pd
from flask import Flask, request, render_template, send_file, jsonify
from werkzeug.utils import secure_filename

# --- Setup Logging ---
logging.basicConfig(
    filename='/home/Certify3/lxp/logs/app.log',
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(message)s'
)

# --- Flask Application Setup ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.expanduser('/home/Certify3'), 'lxp/data')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit

# Chunk size for processing large files
CHUNK_SIZE = 5000  # Process 5000 rows at a time

# Define output file paths
model_order_list_path = os.path.join(app.config['UPLOAD_FOLDER'], 'model_order_list.csv')
exception_report_path = os.path.join(app.config['UPLOAD_FOLDER'], 'exception_report.csv')

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
    if not is_valid_uk_postcode(row["Postcode"]):
        return True
    
    return False

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
        "AH SKU name",   # Uppercase version
        "AH Sku name",   # Lowercase version (from mapping file)
        "AH Pack name"   # Package name
    ]
    order_cols = [col for col in possible_order_cols if col in df.columns]
    
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

def process_chunk(chunk_df, mapping_df, chunk_num):
    """
    Process a single chunk of raw data.
    Returns: (valid_orders_df, exception_orders_df)
    """
    logging.info(f"Processing chunk {chunk_num} with {len(chunk_df)} rows")
    
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
    
    # Define other columns to map
    columns_map = {
        "Customer name": "Customer name",
        "School name": "School name",
        "Email": "Email",
        "End date": "End date",
        "Address": "Address",
        "Postcode": "Postcode",
        "ALDS subscription product name": "ALDS subscription product name",
        "ALDS subscription product ISBN": "ALDS subscription product ISBN",
        "AH SKU name": "AH SKU name",
        "AH Sku name": "AH Sku name",  # Handle both capitalizations
        "AH Pack name": "AH Pack name",
        "Renewal Declined": "Renewal Declined"
    }
    
    # Select columns efficiently
    for new_col, source_col in columns_map.items():
        if source_col in merged_df.columns:
            model_order_list[new_col] = merged_df[source_col]
    
    # Log which mapping columns were found
    if chunk_num == 1:
        found_cols = [col for col in ["AH SKU name", "AH Sku name", "AH Pack name"] if col in model_order_list.columns]
        missing_cols = [col for col in ["AH SKU name", "AH Sku name", "AH Pack name"] if col not in merged_df.columns]
        logging.info(f"Mapping columns found in output: {found_cols}")
        if missing_cols:
            logging.warning(f"Mapping columns NOT found in merged data: {missing_cols}")
    
    # Format school key
    model_order_list["School key"] = merged_df["School key"].apply(format_school_key)
    
    # Add pricing and product info
    model_order_list["AH Line 1 Price"] = merged_df["ISBN ExVAT List Price"]
    model_order_list["AH ISBN 1"] = merged_df["AH Sub 1 ISBN"]
    model_order_list["AH SKU 1"] = merged_df["AH Sub 1 SKU"]
    model_order_list["AH Package 1"] = merged_df["AH Sub 1 Package"]

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
        
        # Get total rows for progress tracking
        total_rows = sum(1 for _ in open(raw_file_path, encoding='latin1')) - 1  # Subtract header
        logging.info(f"Processing {total_rows} total rows in chunks of {CHUNK_SIZE}")
        
        # Initialize result lists
        valid_chunks = []
        exception_chunks = []
        
        chunk_num = 0
        
        # Process file in chunks
        # Use utf-8-sig to automatically strip BOM (Byte Order Mark)
        for chunk_df in pd.read_csv(raw_file_path, encoding='utf-8-sig', chunksize=CHUNK_SIZE, low_memory=False):
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
@app.route("/", methods=["GET", "POST"])
def upload_files():
    if request.method == "POST":
        try:
            # Validate file uploads
            if "raw_data" not in request.files or "mapping_data" not in request.files:
                return "Missing file upload", 400

            raw_file = request.files["raw_data"]
            mapping_file = request.files["mapping_data"]
            exclude_file = request.files.get("exclude_data")

            if not raw_file.filename or not mapping_file.filename:
                return "No file selected", 400

            # Save uploaded files
            raw_file_path = os.path.join(app.config['UPLOAD_FOLDER'], "raw_data.csv")
            mapping_file_path = os.path.join(app.config['UPLOAD_FOLDER'], "example_mapping.csv")
            exclude_csv_path = None
            
            raw_file.save(raw_file_path)
            mapping_file.save(mapping_file_path)
            
            if exclude_file and exclude_file.filename:
                exclude_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], "Exclude.csv")
                exclude_file.save(exclude_csv_path)

            # Read mapping file (small, load fully)
            # Use utf-8-sig to automatically strip BOM (Byte Order Mark)
            logging.info("Reading mapping file...")
            mapping_df = pd.read_csv(mapping_file_path, encoding='utf-8-sig', low_memory=False)
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
            
            # Verify what was actually saved
            saved_df = pd.read_csv(model_order_list_path, encoding='utf-8-sig', nrows=1)
            logging.info(f"Columns in saved CSV: {saved_df.columns.tolist()}")
            logging.info(f"'Sub ID' in saved CSV: {'Sub ID' in saved_df.columns}")
            
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

        except Exception as e:
            logging.error(f"Upload error: {str(e)}")
            return f"Processing error: {e}", 500

    return render_template("upload.html")

@app.route("/download_valid")
def download_valid():
    """Download the valid Model Order List CSV."""
    return send_file(model_order_list_path,
                     as_attachment=True,
                     download_name="model_order_list.csv",
                     mimetype="text/csv")

@app.route("/download_exception")
def download_exception():
    """Download the Exception Report CSV."""
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
        logging.info(f"Checking for name columns: AH SKU name={('AH SKU name' in df.columns)}, AH Sku name={('AH Sku name' in df.columns)}, AH Pack name={('AH Pack name' in df.columns)}")
        
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
    """Test ISBN consistency across files."""
    raw_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], "raw_data.csv")
    mapping_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], "example_mapping.csv")
    
    result, message = test_als_isbn_consistency(raw_csv_path, mapping_csv_path, model_order_list_path)
    return render_template("test_results.html", result=result, message=message)

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
    app.run(debug=True, threaded=True)
