"""
Tests for file utility module
"""

import pytest
import os
import platform
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import util.file as fs


class TestDefinePaths:
    """Tests for path definition functions"""

    def test_define_paths_creates_all_directories(self, tmp_path):
        """Test that _define_paths creates all expected directory paths"""
        fs._define_paths(tmp_path)
        
        # Check that BASE_DIR is set correctly
        assert fs.BASE_DIR == tmp_path
        
        # Check that DATA_DIR is created
        assert fs.DATA_DIR == tmp_path / "data"
        
        # Check various MRMS directories
        assert fs.MRMS_RALA_DIR == tmp_path / "data" / "MRMS_ReflectivityAtLowestAltitude"
        assert fs.MRMS_PROBSEVERE_DIR == tmp_path / "data" / "MRMS_ProbSevere"
        assert fs.MRMS_COMPOSITE_DIR == tmp_path / "data" / "MRMS_MergedReflectivityQC"
        
        # Check output directories
        assert fs.STORMCELL_DIR == tmp_path / "data" / "stormcells"
        assert fs.CELL_DIR == tmp_path / "data" / "cells"
        assert fs.METAR_DIR == tmp_path / "data" / "METAR"
        assert fs.GUI_DIR == tmp_path / "gui"
        assert fs.GUI_RALA_DIR == tmp_path / "gui" / "MRMS_ReflectivityAtLowestAltitude"
        assert fs.WPC_SFC_DIR == tmp_path / "wpc" / "surface_analysis"


class TestInitializeFilesystem:
    """Tests for initialize_filesystem function"""

    def test_initialize_with_custom_base_dir(self, tmp_path):
        """Test initializing with a custom base directory"""
        custom_path = tmp_path / "custom"
        custom_path.mkdir()
        
        fs.initialize_filesystem(str(custom_path))
        
        assert fs.BASE_DIR == custom_path

    def test_initialize_with_path_object(self, tmp_path):
        """Test initializing with a Path object"""
        fs.initialize_filesystem(tmp_path)
        
        assert fs.BASE_DIR == tmp_path


class TestLatestFiles:
    """Tests for latest_files function"""

    def test_returns_n_most_recent_files(self, tmp_path):
        """Test that latest_files returns n most recent files"""
        # Create test files with different modification times
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file3 = tmp_path / "file3.txt"
        
        file1.write_text("content1")
        file2.write_text("content2")
        file3.write_text("content3")
        
        # Set modification times (file3 newest, file1 oldest)
        now = datetime.now()
        os.utime(file1, (now.timestamp() - 300, now.timestamp() - 300))
        os.utime(file2, (now.timestamp() - 200, now.timestamp() - 200))
        os.utime(file3, (now.timestamp() - 100, now.timestamp() - 100))
        
        result = fs.latest_files(tmp_path, 2)
        
        assert len(result) == 2
        assert str(file2) in result
        assert str(file3) in result
        assert str(file1) not in result

    def test_returns_sorted_oldest_to_newest(self, tmp_path):
        """Test that files are sorted oldest to newest"""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file3 = tmp_path / "file3.txt"
        
        file1.write_text("content1")
        file2.write_text("content2")
        file3.write_text("content3")
        
        now = datetime.now()
        os.utime(file1, (now.timestamp() - 300, now.timestamp() - 300))
        os.utime(file2, (now.timestamp() - 200, now.timestamp() - 200))
        os.utime(file3, (now.timestamp() - 100, now.timestamp() - 100))
        
        result = fs.latest_files(tmp_path, 3)
        
        # Should be sorted oldest to newest
        assert result[0] == str(file1)
        assert result[1] == str(file2)
        assert result[2] == str(file3)

    def test_excludes_idx_files(self, tmp_path):
        """Test that .idx files are excluded"""
        file1 = tmp_path / "file1.txt"
        idx_file = tmp_path / "file1.idx"
        file2 = tmp_path / "file2.txt"
        
        file1.write_text("content1")
        idx_file.write_text("index")
        file2.write_text("content2")
        
        result = fs.latest_files(tmp_path, 10)
        
        assert str(file1) in result
        assert str(file2) in result
        assert str(idx_file) not in result

    def test_excludes_partial_files(self, tmp_path):
        complete_file = tmp_path / "MRMS_20260419-001000.grib2"
        partial_file = tmp_path / ".MRMS_20260419-001200.grib2.part"
        complete_file.write_text("complete")
        partial_file.write_text("partial")

        result = fs.latest_files(tmp_path, 10)

        assert result == [str(complete_file)]

    def test_returns_none_for_nonexistent_directory(self):
        """Test that None is returned for non-existent directory"""
        non_existent = Path("/non/existent/path")
        
        result = fs.latest_files(non_existent, 5)
        
        assert result is None

    def test_handles_empty_directory(self, tmp_path):
        """Test handling of empty directory"""
        result = fs.latest_files(tmp_path, 5)
        
        assert result == []


class TestCleanFilesByAge:
    """Tests for clean_files_by_age function"""

    def test_removes_old_files(self, tmp_path):
        """Test that old files are removed"""
        fs._define_paths(tmp_path)
        old_file = tmp_path / "old_file.txt"
        new_file = tmp_path / "new_file.txt"
        
        old_file.write_text("old content")
        new_file.write_text("new content")
        
        # Set old file modification time to 3 hours ago
        old_time = datetime.now() - timedelta(hours=3)
        os.utime(old_file, (old_time.timestamp(), old_time.timestamp()))
        
        # Set new file modification time to 30 minutes ago
        new_time = datetime.now() - timedelta(minutes=30)
        os.utime(new_file, (new_time.timestamp(), new_time.timestamp()))
        
        fs.clean_files_by_age(tmp_path, max_age_minutes=120)
        
        assert not old_file.exists()
        assert new_file.exists()

    def test_keeps_recent_files(self, tmp_path):
        """Test that recent files are kept"""
        fs._define_paths(tmp_path)
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        
        file1.write_text("content1")
        file2.write_text("content2")
        
        # Both files are 30 minutes old
        mod_time = datetime.now() - timedelta(minutes=30)
        os.utime(file1, (mod_time.timestamp(), mod_time.timestamp()))
        os.utime(file2, (mod_time.timestamp(), mod_time.timestamp()))
        
        fs.clean_files_by_age(tmp_path, max_age_minutes=120)
        
        assert file1.exists()
        assert file2.exists()

    def test_handles_nonexistent_directory(self):
        """Test handling of non-existent directory"""
        non_existent = Path("/non/existent/path")
        
        # Should not raise an error
        fs.clean_files_by_age(non_existent, max_age_minutes=120)


class TestCleanOldFiles:
    """Tests for clean_old_files function"""
    
    @pytest.fixture(autouse=True)
    def setup_base_dir(self, tmp_path):
        """Ensure BASE_DIR is set to tmp_path for these tests"""
        original_base = fs.BASE_DIR
        fs.BASE_DIR = tmp_path
        yield
        fs.BASE_DIR = original_base

    def test_removes_oldest_files_when_limit_exceeded(self, tmp_path):
        """Test that oldest files are removed when exceeding limit"""
        fs._define_paths(tmp_path)
        files = []
        for i in range(5):
            f = tmp_path / f"file{i}.txt"
            f.write_text(f"content{i}")
            files.append(f)
            
            # Set modification time (older for lower indices)
            mod_time = datetime.now() - timedelta(minutes=(5-i)*10)
            os.utime(f, (mod_time.timestamp(), mod_time.timestamp()))
        
        fs.clean_old_files(tmp_path, max_files=3)
        
        # Oldest files (file0, file1) should be removed
        assert not files[0].exists()
        assert not files[1].exists()
        # Newer files should remain
        assert files[2].exists()
        assert files[3].exists()
        assert files[4].exists()

    def test_keeps_all_files_when_under_limit(self, tmp_path):
        """Test that all files are kept when under limit"""
        fs._define_paths(tmp_path)
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.txt"
            f.write_text(f"content{i}")
            files.append(f)
        
        fs.clean_old_files(tmp_path, max_files=5)
        
        for f in files:
            assert f.exists()

    def test_handles_nonexistent_directory(self):
        """Test handling of non-existent directory"""
        non_existent = Path("/non/existent/path")
        
        # Should not raise an error
        fs.clean_old_files(non_existent, max_files=10)

    def test_excludes_idx_files_from_count(self, tmp_path):
        """Test that .idx files are excluded from file count"""
        fs._define_paths(tmp_path)
        txt_file = tmp_path / "file.txt"
        idx_file = tmp_path / "file.idx"
        
        txt_file.write_text("content")
        idx_file.write_text("index")
        
        fs.clean_old_files(tmp_path, max_files=1)
        
        # Both should remain since only 1 txt file
        assert txt_file.exists()
        assert idx_file.exists()
