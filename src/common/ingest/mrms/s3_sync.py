import heapq
import os
import gzip
import shutil
from functools import lru_cache
from pathlib import Path
from datetime import timedelta

import boto3
from botocore import UNSIGNED
from botocore.client import Config

from util.handler import extract_timestamp
from common.ingest.mrms.config import mrms_decompress_chunk_size_bytes
from common.ingest.mrms.s3_common import select_target_file


@lru_cache(maxsize=1)
def _get_unsigned_s3_client():
    return boto3.client('s3', config=Config(signature_version=UNSIGNED))

class FileFinder:
    __slots__ = ("dt", "bucket", "max_entries", "io_manager", "client", "paginator")

    def __init__(self, dt, bucket, max_entries, io_manager, client=None):
        self.dt = dt
        self.bucket = bucket
        self.max_entries = max_entries  # Maximum number of entries to return
        self.io_manager = io_manager # Use the IOManager class in util.io
        self.client = client if client is not None else _get_unsigned_s3_client()
        self.paginator = self.client.get_paginator('list_objects_v2')
    
    def lookup_files(self, modifier, verbose=False, start_after=None):
        """
        Look up latest S3 files and return as list of (path, datetime_obj) tuples.
        
        Args:
            modifier (str | list[str]): Specify which part(s) of the bucket to search (e.g., folder prefix).
                                      Can be a single string or a list of strings to search sequentially.
            verbose (bool): Whether to print debug information
            start_after (str): optional S3 StartAfter key for pagination
        
        Uses S3 client and instance variables to find and filter files.
        Returns files sorted by timestamp in descending order (latest first).
        
        Returns:
            list: List of tuples (s3_path, datetime_obj) sorted by latest timestamp first
        """
        try:
            # Normalize modifier to list
            modifiers = [modifier] if isinstance(modifier, str) else modifier
            
            top_files = []
            
            push = heapq.heappush
            replace = heapq.heapreplace
            max_entries = self.max_entries

            for prefix in modifiers:
                # Set up prefix filter for bucket search
                search_prefix = prefix if prefix else ""
                
                # Custom pagination config
                kwargs = {"Bucket": self.bucket, "Prefix": search_prefix}
                if start_after:
                    kwargs["StartAfter"] = start_after

                # Iterate through all pages of results
                for page in self.paginator.paginate(**kwargs):
                    if 'Contents' in page:
                        for obj in page['Contents']:
                            s3_path = obj['Key']

                            try:
                                # Extract timestamp from S3 path
                                timestamp = extract_timestamp(s3_path, use_timezone_utc=True, round_to_minute=False, isoformat=False)

                                if timestamp.replace(second=0, microsecond=0) > self.dt:
                                    continue

                                entry = (timestamp, s3_path)
                                if len(top_files) < max_entries:
                                    push(top_files, entry)
                                elif entry[0] > top_files[0][0]:
                                    replace(top_files, entry)
                            except Exception:
                                # Skip files that don't have valid timestamps
                                continue

                # Optimization: If we have enough files, stop searching subsequent modifiers
                # We only check this after finishing a prefix to ensure we get all files from that prefix
                # (e.g. all files from the current hour) before deciding if we need more.
                if len(top_files) >= self.max_entries:
                    break

            # Sort by timestamp (latest first)
            top_files.sort(key=lambda x: x[0], reverse=True)

            # Limit to max_entries
            return [(path, ts) for ts, path in top_files[:self.max_entries]]
            
        except Exception as e:
            # Log error and return empty list
            self.io_manager.write_error(f"Error looking up files: {e}")
            return []

class FileDownloader:
    __slots__ = ("dt", "bucket", "io_manager", "target_minute", "target_key", "client")

    def __init__(self, dt, bucket, io_manager, client=None):
        self.dt = dt
        self.bucket = bucket
        self.io_manager = io_manager # IOManager class from util.io
        self.target_minute = dt.replace(second=0, microsecond=0)
        self.target_key = (dt.year, dt.month, dt.day, dt.hour, dt.minute)
        self.client = client if client is not None else _get_unsigned_s3_client()

    def _select_target_file(self, file_list):
        """
        Select the file that best matches the target datetime.

        Delegates to :func:`~common.ingest.mrms.s3_common.select_target_file`.
        """
        return select_target_file(self.dt, file_list, self.io_manager)


    def download_matching(self, file_list, outdir: Path):
        """
        Download the file that matches the target datetime.
        
        Args:
            file_list (list): List of tuples (s3_path, datetime_obj) from FileFinder.lookup_files()
            outdir (Path): Output directory path where the file will be downloaded
            
        Returns:
            Path: Path to the downloaded file, or None if download failed
        """
        if not file_list:
            self.io_manager.write_warning("No files to download from empty file_list")
            return None
        
        try:
            # Find file with matching timestamp
            target_file_path = self._select_target_file(file_list)
            
            # Create output directory if it doesn't exist
            outdir = Path(outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            
            # Extract filename from S3 path
            filename = os.path.basename(target_file_path)
            local_path = outdir / filename
            
            # Check if file already exists (both zipped and unzipped versions)
            zipped_path = local_path
            unzipped_path = local_path.with_suffix("") if local_path.suffix == ".gz" else local_path
            
            if zipped_path.exists() or unzipped_path.exists():
                existing_file = str(unzipped_path) if unzipped_path.exists() else str(zipped_path)
                self.io_manager.write_debug(f"File already exists, skipping download: {existing_file}")
                return unzipped_path if unzipped_path.exists() else zipped_path

            # Use the bucket from constructor and the file path as S3 key
            s3_key = target_file_path
            
            # Download the file from S3
            part_path = local_path.with_name(f".{local_path.name}.part")
            try:
                self.client.download_file(self.bucket, s3_key, str(part_path))
                if not part_path.is_file() or part_path.stat().st_size == 0:
                    raise IOError("empty S3 download")
                os.replace(part_path, local_path)
            except BaseException:
                part_path.unlink(missing_ok=True)
                raise
            
            return Path(str(local_path))
            
        except Exception as e:
            self.io_manager.write_error(f"Error downloading matching file from {self.bucket}: {e}")
            return None

    def download_all_matching(self, file_list, outdir: Path):
        """
        Download all files that match the target datetime minute (sliding window).
        
        Args:
            file_list (list): List of tuples (s3_path, datetime_obj) from FileFinder.lookup_files()
            outdir (Path): Output directory path where the files will be downloaded
            
        Returns:
            list[Path]: List of paths to the downloaded files
        """
        if not file_list:
            self.io_manager.write_warning("No files to download from empty file_list")
            return []
        
        downloaded_files = []
        
        try:
            # Sliding window logic:
            # Target window is (dt - 1 minute, dt]
            # e.g. if dt is 3:39:30, we want files > 3:38:30 and <= 3:39:30
            window_end = self.dt
            window_start = window_end - timedelta(minutes=1)
            
            matching_files = [
                s3_path for s3_path, ts in file_list 
                if window_start < ts <= window_end
            ]
            
            if not matching_files:
                self.io_manager.write_warning(f"No files found matching window {window_start} to {window_end}.")
                return []

            # Create output directory if it doesn't exist
            outdir = Path(outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            
            for target_file_path in matching_files:
                # Extract filename from S3 path
                filename = os.path.basename(target_file_path)
                local_path = outdir / filename
                
                # Check if file already exists (both zipped and unzipped versions)
                zipped_path = local_path
                unzipped_path = local_path.with_suffix("") if local_path.suffix == ".gz" else local_path
                
                if zipped_path.exists() or unzipped_path.exists():
                    existing_file = str(unzipped_path) if unzipped_path.exists() else str(zipped_path)
                    self.io_manager.write_debug(f"File already exists, skipping download: {existing_file}")
                    downloaded_files.append(unzipped_path if unzipped_path.exists() else zipped_path)
                    continue

                # Use the bucket from constructor and the file path as S3 key
                s3_key = target_file_path
                
                # Download the file from S3
                part_path = local_path.with_name(f".{local_path.name}.part")
                try:
                    self.client.download_file(self.bucket, s3_key, str(part_path))
                    if not part_path.is_file() or part_path.stat().st_size == 0:
                        raise IOError("empty S3 download")
                    os.replace(part_path, local_path)
                except BaseException:
                    part_path.unlink(missing_ok=True)
                    raise
                
                downloaded_files.append(Path(str(local_path)))
            
            return downloaded_files
            
        except Exception as e:
            self.io_manager.write_error(f"Error downloading matching files from {self.bucket}: {e}")
            return downloaded_files

    def decompress_file(self, gz_path: Path) -> Path | None:
        """
        Decompress a .gz file into its parent directory and delete the original .gz.
        """
        if not gz_path.exists():
            self.io_manager.write_error(f"File does not exist: {gz_path}")
            return None

        if gz_path.suffix != ".gz":
            self.io_manager.write_warning(f"Not a .gz file: {gz_path}")
            return None

        try:
            # Decompressed file path (remove .gz)
            output_path = gz_path.with_suffix("")

            if output_path.exists():
                gz_path.unlink(missing_ok=True)
                self.io_manager.write_debug(f"Decompressed target already exists, skipping: {output_path}")
                return output_path

            part_path = output_path.with_name(f".{output_path.name}.part")
            try:
                # Reading through EOF validates the gzip stream before commit.
                with gzip.open(gz_path, "rb") as f_in, open(part_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out, length=mrms_decompress_chunk_size_bytes())
                    f_out.flush()
                    os.fsync(f_out.fileno())
                if part_path.stat().st_size == 0:
                    raise IOError("decompressed output is empty")
                os.replace(part_path, output_path)
            except BaseException:
                part_path.unlink(missing_ok=True)
                raise

            # Remove original gz file
            gz_path.unlink(missing_ok=True)

            return output_path
        
        except Exception as e:
            self.io_manager.write_error(f"Unable to decompress {gz_path}: {e}")
            return None
