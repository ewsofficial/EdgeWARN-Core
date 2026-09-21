
import aiohttp
import asyncio
import requests
import re
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
import logging
import os

# We'll use the existing IOManager if possible, or fallback to logging
try:
    from util.io import IOManager
    io_manager = IOManager("[Ingest-HTTPS]")
except ImportError:
    logging.basicConfig(level=logging.INFO)
    io_manager = logging.getLogger("[Ingest-HTTPS]")
    # Add write_info/write_error methods to simulate IOManager
    io_manager.write_info = io_manager.info
    io_manager.write_error = io_manager.error
    io_manager.write_warning = io_manager.warning
    io_manager.write_debug = io_manager.debug

from common.ingest.mrms.config import (
    ncep_base_url,
    ncep_directory_map,
    ncep_directory_split_token,
    ncep_download_chunk_size_bytes,
    ncep_match_window_seconds,
    ncep_probsevere_url,
    ncep_sync_timeout_seconds,
)

# No NCEP_BASE_URL constant: run.py imports this package before get_args() exports
# EDGEWARN_CONFIG_DIR, so a module-scope binding would freeze the repo-default
# catalog and --config-dir could never reach it. construct_url resolves per call.


class HttpsFileFinder:
    def __init__(self, dt, io_manager_instance=None):
        self.dt = dt
        self.io_manager = io_manager_instance or io_manager
        self.session = None

    def _get_product_url_name(self, modifier):
        """
        Maps S3 modifier keywords to NCEP URL directory names.
        This is necessary because the S3 modifiers might slightly differ or
        we just need to extract the base product name.

        Example:
        S3: "EchoTop_18_00.50" -> NCEP: "EchoTop_18"
        S3: "ProbSevere" -> NCEP: "ProbSevere" (handled separately usually)

        The map and the split-token fallback both come from
        `mrms.ncep_https` in ingest.yaml, so a directory rename upstream is an
        operator edit rather than a code change. Most names derive by dropping the
        level suffix; the map exists for the ones that do not, and the NCEP index
        keeps VIL/, VIL_Density/ and LVL3_HighResVIL/ as distinct directories, so
        those products must not be collapsed onto one another.
        """
        if modifier is None: # ProbSevere
            return "ProbSevere" # The actual URL is /data/ProbSevere, handled in construct_url

        mapping = ncep_directory_map()
        if modifier in mapping:
            return mapping[modifier]

        # Fallback default behaviors if not mapped
        parts = modifier.split(ncep_directory_split_token())
        if len(parts) > 1:
            return parts[0]

        return modifier

    def construct_url(self, region, modifier):
        """Constructs the NCEP URL. Note: MRMS 2D data on NCEP is flat, not organized by date folders like S3."""
        prod_name = self._get_product_url_name(modifier)

        if modifier is None: # ProbSevere
            # A sibling of /data/2D, not a directory inside it, hence its own key.
            return ncep_probsevere_url()

        # Standard 2D products
        return f"{ncep_base_url()}/{prod_name}"

    async def find_files(self, region, modifier):
        """
        Scrapes the NCEP directory for files matching the requested timestamp (self.dt).
        Since NCEP only keeps recent files, we just look for the closest match.
        """
        url = self.construct_url(region, modifier)
        target_ts_str = self.dt.strftime("%Y%m%d-%H%M")
        
        self.io_manager.write_debug(f"Scanning {url} for {target_ts_str}...")
        
        # Standard SSL verification is now used as testing confirmed support
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        self.io_manager.write_warning(f"Failed to access {url}: HTTP {response.status}")
                        return []
                    
                    html = await response.text()
            except Exception as e:
                self.io_manager.write_error(f"Error scraping {url}: {e}")
                return []

        soup = BeautifulSoup(html, 'html.parser')
        links = soup.find_all('a')
        
        valid_files = []
        for link in links:
            href = link.get('href')
            if not href.endswith('.gz') and not href.endswith('.json'):
                continue
                
            # Name format: MRMS_{Product}_{Level}_{YYYYMMDD-HHMMSS}.grib2.gz
            # Check if it matches our target time window? or just return the list so the downloader can pick?
            # The S3 logic usually picks the *exact* or *closest* match. 
            # Let's filter by at least the hour to narrow it down if possible, 
            # but NCEP usually only holds the last 24h or so, so we can just grab everything 
            # and let the logic filter for the specific timestamp.
            
            # Simple filtering: Check if the date part matches at least the day?
            # "20260124"
            if self.dt.strftime("%Y%m%d") in href:
                valid_files.append(f"{url}/{href}")
                
        return valid_files

    def find_files_sync(self, region, modifier, *, log_scan=True):
        """
        Sync version of find_files using requests (for scheduler).
        """
        url = self.construct_url(region, modifier)
        target_ts_str = self.dt.strftime("%Y%m%d-%H%M")
        
        if log_scan:
            self.io_manager.write_debug(f"Scanning (Sync) {url} for {target_ts_str}...")
        
        try:
            response = requests.get(url, timeout=ncep_sync_timeout_seconds())
            if response.status_code != 200:
                self.io_manager.write_warning(f"Failed to access {url}: HTTP {response.status_code}")
                return []
            html = response.text
        except Exception as e:
            self.io_manager.write_error(f"Error scraping {url}: {e}")
            return []

        soup = BeautifulSoup(html, 'html.parser')
        links = soup.find_all('a')
        
        valid_files = []
        for link in links:
            href = link.get('href')
            if not href.endswith('.gz') and not href.endswith('.json'):
                continue

            if self.dt.strftime("%Y%m%d") in href:
                valid_files.append(f"{url}/{href}")
                
        return valid_files


class HttpsFileDownloader:
    def __init__(self, dt, io_manager_instance=None):
        self.dt = dt
        self.io_manager = io_manager_instance or io_manager

    async def download_matching(self, file_urls, outdir):
        """
        Given a list of file URLs, find the one matching self.dt (minute precision) 
        and download it.
        """
        match = self._select_matching_url(file_urls)

        if not match:
            return None

        # Download
        filename = match.split('/')[-1]
        outdir.mkdir(parents=True, exist_ok=True)
        out_path = outdir / filename
        
        if out_path.exists():
            # Already exists
            return out_path

        self.io_manager.write_info(f"Downloading (HTTPS Fallback): {filename}")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(match) as response:
                    if response.status == 200:
                        part_path = out_path.with_name(f".{out_path.name}.part")
                        chunk_size = ncep_download_chunk_size_bytes()
                        written = 0
                        try:
                            with open(part_path, 'wb') as f:
                                while True:
                                    chunk = await response.content.read(chunk_size)
                                    if not chunk:
                                        break
                                    written += len(chunk)
                                    f.write(chunk)
                                f.flush()
                            expected = response.content_length
                            if expected is not None and written != expected:
                                raise IOError(f"incomplete HTTPS download: expected {expected} bytes, got {written}")
                            if written == 0:
                                raise IOError("empty HTTPS download")
                            part_path.replace(out_path)
                        except BaseException:
                            part_path.unlink(missing_ok=True)
                            raise
                        return out_path
                    else:
                        self.io_manager.write_error(f"Failed to download {match}: {response.status}")
                        return None
            except Exception as e:
                self.io_manager.write_error(f"Download error {match}: {e}")
                return None

    def _select_matching_url(self, file_urls):
        """Apply the same exact-minute/limited-offset rule to both transports."""
        target_ts = self.dt.strftime("%Y%m%d-%H%M")
        for url in file_urls:
            if target_ts in url.replace(":", ""):
                return url
        matches = []
        target_dt = self.dt if self.dt.tzinfo else self.dt.replace(tzinfo=timezone.utc)
        for url in file_urls:
            found = re.search(r'(\d{8}-\d{6})', url)
            if found is None:
                continue
            try:
                file_dt = datetime.strptime(found.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            diff = abs((file_dt - target_dt).total_seconds())
            if diff < ncep_match_window_seconds():
                matches.append((diff, url))
        return min(matches)[1] if matches else None

    def download_matching_sync(self, file_urls, outdir):
        match = self._select_matching_url(file_urls)
        if match is None:
            return None
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        out_path = outdir / match.rsplit('/', 1)[-1]
        if out_path.is_file():
            return out_path
        part_path = out_path.with_name(f".{out_path.name}.part")
        try:
            with requests.get(match, timeout=ncep_sync_timeout_seconds(), stream=True) as response:
                response.raise_for_status()
                written = 0
                with open(part_path, 'wb') as handle:
                    for chunk in response.iter_content(chunk_size=ncep_download_chunk_size_bytes()):
                        if chunk:
                            written += len(chunk)
                            handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                expected = response.headers.get('Content-Length')
                if written == 0 or (expected is not None and written != int(expected)):
                    raise IOError(f"incomplete HTTPS download: expected {expected} bytes, got {written}")
            part_path.replace(out_path)
            return out_path
        except Exception as exc:
            self.io_manager.write_error(f"HTTPS download error {match}: {exc}")
            return None
        finally:
            part_path.unlink(missing_ok=True)
