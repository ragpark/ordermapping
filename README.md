# Order Mapping Tool

A Flask web application for mapping ALS (Active Learning Solutions) subscriptions to AH (Active Hub) products. Processes large CSV files efficiently using chunked data processing.

## Features

- **Chunked Processing**: Handles large CSV files (100MB+) efficiently
- **Data Validation**: UK postcode and email validation
- **ISBN Mapping**: Maps raw subscription data to product codes
- **Exception Reporting**: Identifies and reports invalid records
- **Order Grouping**: Groups multiple orders by subscription ID
- **Exclusion Support**: Optional filtering based on exclusion lists
- **Railway-Optimized**: Configured for Railway.app deployment

## File Processing Workflow

1. **Upload Files**:
   - Raw subscription data (CSV)
   - Product mapping data (CSV)
   - Exclusion list (CSV, optional)

2. **Processing**:
   - Validates data format and business rules
   - Maps ISBNs to product information
   - Applies exclusions if provided
   - Generates valid orders and exception reports

3. **Download Results**:
   - Model Order List (valid orders)
   - Exception Report (invalid/excluded orders)
   - Grouped Orders (consolidated by Sub ID)

## Installation

### Local Development

```bash
git clone <repository-url>
cd ordermapping
pip install -r requirements.txt
python app.py
```

### Railway Deployment

1. **Connect Repository**:
   ```bash
   # Option 1: Railway CLI
   npm install -g @railway/cli
   railway login
   railway init
   railway up

   # Option 2: GitHub Integration
   # Connect via Railway dashboard → Deploy from GitHub
   ```

2. **Set Environment Variables** (optional):
   ```bash
   railway variables set SECRET_KEY="your-secret-key"
   railway variables set CHUNK_SIZE="5000"
   railway variables set LOG_LEVEL="INFO"
   ```

3. **Access Application**:
   Railway will provide a URL like: `https://your-app.railway.app`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Server port (Railway sets automatically) |
| `FLASK_ENV` | `production` | Flask environment |
| `SECRET_KEY` | `dev-secret-key` | Flask secret key |
| `UPLOAD_FOLDER` | `/tmp/uploads` | File upload directory |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FILE` | `/tmp/app.log` | Log file path |
| `CHUNK_SIZE` | `5000` | CSV processing chunk size |

## CSV File Requirements

### Raw Data CSV
Required columns:
- `ISBN` - Product identifier for mapping
- `Sub ID` - Subscription identifier (for grouping)
- `School key` - School identifier
- `School name` - Institution name
- `Customer name` - Customer name
- `Email` - Contact email (validated)
- `Postcode` - UK postcode (validated)
- `Address` - Physical address
- `End date` - Subscription end date

### Mapping CSV
Required columns:
- `ALDS subscription product ISBN` - Maps to raw data ISBN
- `ALDS subscription product name` - Product name
- `AH Sub 1 ISBN`, `AH Sub 1 SKU`, `AH Sub 1 Package` - Product codes
- `ISBN ExVAT List Price` - Pricing information
- `AH SKU name`, `AH Pack name`, `AH Sub Package Name` - Product descriptions

### Exclusion CSV (Optional)
Should contain one of:
- `Sub ID`, `School key`, `ISBN`, or `ALDS subscription product ISBN`

## API Endpoints

- `GET /` - Upload form
- `POST /` - Process uploaded files
- `GET /download_valid` - Download valid orders CSV
- `GET /download_exception` - Download exception report CSV
- `GET /download_grouped` - Download grouped orders CSV
- `GET /test_als_isbn` - Test ISBN consistency
- `GET /debug_columns` - Debug column information
- `GET /health` - Health check (Railway)

## Business Rules

### Validation Criteria
Records are marked as exceptions if:
- Missing required fields (School key, name, email, postcode, product codes)
- Invalid email format
- Invalid UK postcode format
- Renewal declined = "yes"

### Data Processing
- School keys padded to 7 digits with leading zeros
- ISBNs normalized to consistent string format
- Excel formula wrapping removed from school keys
- Chunked processing for memory efficiency

## Technical Architecture

- **Framework**: Flask 2.3.3
- **Data Processing**: pandas with chunked reading
- **Deployment**: Railway.app with gunicorn
- **Storage**: Ephemeral (files cleared on redeploy)
- **Logging**: File + stdout (Railway captures stdout)

## Troubleshooting

### Common Issues

1. **"Sub ID column missing"**: Raw data file lacks Sub ID column
2. **"No common ISBN found"**: No matching ISBNs between files
3. **Memory errors**: Reduce `CHUNK_SIZE` environment variable
4. **File not found**: Download files immediately after processing

### Debug Endpoints

- `/debug_columns` - Shows available columns in uploaded files
- `/test_als_isbn` - Verifies ISBN matching across all files
- `/health` - Application status and configuration

## Railway-Specific Notes

- **File Storage**: Uses `/tmp/uploads` (ephemeral storage)
- **Memory**: Default 1GB RAM (chunked processing optimized for this)
- **Disk Space**: Limited temporary storage, files cleaned between deploys
- **Logs**: Accessible via `railway logs` command
- **Health Checks**: Built-in health endpoint for Railway monitoring

## Development

### Adding Features
1. Add route handlers in `app.py`
2. Create templates in `templates/`
3. Update requirements in `requirements.txt`
4. Test locally before deploying

### Testing
```bash
# Local testing with Railway-like environment
export FLASK_ENV=production
export UPLOAD_FOLDER=/tmp/uploads
mkdir -p /tmp/uploads
python app.py
```

## License

Internal Pearson tool - see corporate license agreement.
