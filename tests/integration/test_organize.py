from organizer import organize_directory


def test_photos_land_in_dated_subfolder(tmp_path, make_jpeg_file):
    make_jpeg_file("a.jpg", date_str="2024:06:01 10:00:00")
    make_jpeg_file("b.jpg", date_str="2024:06:01 11:00:00")
    organize_directory(str(tmp_path))
    event = tmp_path / "2024-06-01"
    assert event.is_dir()
    assert len(list(event.glob("*.jpg"))) == 2


def test_trip_folder_created_for_nearby_gps(tmp_path, make_jpeg_file):
    make_jpeg_file("d1.jpg", date_str="2024:03:10 09:00:00", gps=(35.6, 139.7))
    make_jpeg_file("d2.jpg", date_str="2024:03:11 10:00:00", gps=(35.7, 139.8))
    organize_directory(str(tmp_path))
    assert len(list(tmp_path.glob("Trip_*"))) == 1


def test_corrupt_file_does_not_crash(tmp_path):
    (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\xff\xe0NOTREALJPEG")
    organize_directory(str(tmp_path))  # must not raise


def test_unsupported_extension_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("ignore me")
    organize_directory(str(tmp_path))
    assert (tmp_path / "notes.txt").exists()