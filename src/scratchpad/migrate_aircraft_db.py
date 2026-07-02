import os
import shutil
import time
from pathlib import Path

def copy_with_progress(src: Path, dst: Path):
    if dst.exists():
        src_size = src.stat().st_size
        dst_size = dst.stat().st_size
        if src_size == dst_size:
            print(f"File {dst.name} already exists and has the same size ({src_size / 1024 / 1024:.2f} MB). Skipping copy.")
            return
        else:
            print(f"File {dst.name} exists but size differs (src: {src_size}, dst: {dst_size}). Overwriting...")

    print(f"Copying {src.name} ({src.stat().st_size / 1024 / 1024:.2f} MB)...")
    start_time = time.time()
    
    # Custom block-by-block copy with simple progress report for very large files
    buffer_size = 16 * 1024 * 1024  # 16 MB
    total_size = src.stat().st_size
    copied = 0
    
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, 'rb') as fsrc:
        with open(dst, 'wb') as fdst:
            while True:
                buf = fsrc.read(buffer_size)
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
                print(f"  Progress: {copied / total_size * 100:.1f}% ({copied / 1024 / 1024:.1f} / {total_size / 1024 / 1024:.1f} MB)", end='\r')
    
    elapsed = time.time() - start_time
    print(f"\nFinished copying {src.name} in {elapsed:.2f} seconds.")

def main():
    src_dir = Path(r"G:\Meine Ablage\UNI\SS26\master_flights_builder\data\aircraft_db\MasterFiles")
    dst_dir = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie\data\databases\aircraft_db")
    
    files_to_copy = [
        "aircraft-database-complete-2025-08.csv",
        "doc8643AircraftTypes.csv",
        "openairframes_adsb_2024-01-01_2026-02-23.csv.gz"
    ]
    
    if not src_dir.exists():
        print(f"Source directory {src_dir} does not exist. Cannot migrate aircraft DB.")
        return
        
    for fname in files_to_copy:
        src_path = src_dir / fname
        dst_path = dst_dir / fname
        if src_path.exists():
            copy_with_progress(src_path, dst_path)
        else:
            print(f"Warning: Source file {src_path} not found.")

if __name__ == "__main__":
    main()
