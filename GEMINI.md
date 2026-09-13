# EdgeWARN-Core

## Project Overview

EdgeWARN-Core is the backend server for the EdgeWARN severe weather nowcasting system. It processes meteorological data from various sources (NOAA MRMS, ProbSevere v3, RAP, GOES-19 GLM, NWS Alerts, METAR) to provide real-time and historical weather analysis.

The system consists of three main runtime components:
1.  **Python Core:** Handles data ingestion, processing, and analysis (real-time and historical). Includes the **Context-aware Threat Assessment Module (CTAM)** with built-in StormProb forecasting and externally installed analytics.
2.  **EdgeWARN Node.js API (v2):** An Express.js service that serves processed EdgeWARN data and features.
3.  **EWMRS Node.js API:** An Express.js service that serves rendered products, RAP arrays, NEXRAD intermediates, WPC surface analysis, and colormaps.

### Key Technologies
*   **Python:** 3.13+ (Managed via Conda)
*   **Data Processing:** `numpy`, `xarray`, `scikit-image`, `scipy`, `shapely`, `rasterio`, `netcdf4`, `pyproj`, `opencv-python-headless`
*   **Cloud/Network:** `boto3`, `aioboto3`, `aiohttp`, `aiofiles`
*   **Node.js:** Express.js, CORS, Helmet, Compression, `express-rate-limit`, `lru-cache`

## Building and Running

### Prerequisites
*   Conda or Miniconda
*   npm (Node.js)
*   git

### Installation
1.  Clone the repository:
    ```bash
    git clone https://www.github.com/ewsofficial/EdgeWARN-Core
    cd EdgeWARN-Core
    ```
2.  Create and activate the Conda environment:
    ```bash
    conda env create -f environment.yml
    conda activate EdgeWARN-dev
    ```
3.  Install Node.js dependencies:
    ```bash
    npm install
    ```

### Running the Application

#### 1. Real-Time Analysis
Run the core python script to pull and analyze live data:
```bash
python src/run.py [options]
```
**Options:**
*   `--lat_limits <min> <max>`: Latitude limits
*   `--lon_limits <min> <max>`: Longitude limits
*   `--base_dir <path>` or `--base-dir <path>`: Runtime base directory override (default `~/EdgeWARN_input` on Linux/macOS, `C:\EdgeWARN_input` on Windows)
*   `--profile`: Enable performance profiling
*   `--disable-ctam`: Skip CTAM execution during integration
*   `--disable-tracking`: Skip lineage detection and Kalman tracking
*   `--disable-polygon-expansion`: Skip ProbSevere polygon-to-radar gate mapping
*   `--disable-ewmrs`: Disable EWMRS workers and rendering
*   `--disable-nws`: Disable background NWS alert ingestion
*   `--disable-metar`: Disable background METAR ingestion
*   `--disable-goes`: Disable GOES ingest, GLM ingest, and GOES rendering
*   `--disable-nexrad`: Disable NEXRAD Level II ingestion entirely
*   `--mrms-core-only`: Run MRMS ingestion for EWMRS rendering without waiting on EdgeWARN detection outputs
*   `--refl-threshold <value>`: Override the baseline reflectivity threshold
*   `--min-seed-percentage <value>`: Override polygon seed coverage ratio
*   `--drop-offset <value>`: Override dynamic reflectivity drop offset

#### 2. Historical Analysis
Process historical data for whatever input files are available near the requested timestamps:
```bash
python src/process_historical.py --start <ISO8601> --end <ISO8601> [options]
```

**Options:**
*   `--lat <min> <max>`: Latitude limits
*   `--lon <min> <max>`: Longitude limits
*   `--base_dir <path>` or `--base-dir <path>`: Runtime base directory override
*   `--profile`: Enable performance profiling
*   `--disable-ctam`: Skip CTAM execution during integration
*   `--disable-tracking`: Skip lineage detection and Kalman tracking
*   `--disable-polygon-expansion`: Skip ProbSevere polygon-to-radar gate mapping
*   `--refl-threshold <value>`: Override the baseline reflectivity threshold
*   `--min-seed-percentage <value>`: Override polygon seed coverage ratio
*   `--drop-offset <value>`: Override dynamic reflectivity drop offset

#### 3. API Server (v2)
Start the Node.js backend server:
```bash
npm run api:edgewarn
```
*   **Default Port:** 5000
*   **Debug Port:** 3001 (via `npm run debug:edgewarn`)
*   **Port Override:** `PORT`
*   **Health Check:** `http://localhost:5000/health`
*   **EWMRS API:** `npm run api:ewmrs`
*   **EWMRS Debug API:** `npm run debug:ewmrs`

## Development Conventions

### Branching Strategy
*   **Features:** `<yourname>/feat/<feature-name>`
*   **Bug Fixes:** `<yourname>/fix/<bug-name>`

### Commit Messages
Use the following prefixes:
*   `ADD`: New files/modules
*   `FTR`: New features
*   `IMP`: Improvements
*   `FIX`: Bug fixes
*   `HOT`: Hotfixes
*   `REF`: Refactoring
*   `CLN`: Cleanup
*   `OPT`: Optimization
*   `DOC`: Documentation
*   `STL`: Style changes
*   `TST`: Testing
*   `CIC`: CI/CD
*   `BLD`: Build/Tooling

### Testing
*   **Python:** Run tests using the active Conda environment.
    ```bash
    python -m pytest tests/
    ```
*   **Node.js:** Run tests using `jest`.
    ```bash
    npm test
    ```

### Code Style
*   Follow existing code styles.
*   Ensure no sensitive data (API keys, passwords) is committed.
*   Node.js code uses ES modules (`import`/`export`).
