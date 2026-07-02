import shutil
from pathlib import Path

def main():
    src_reports_dir = Path(r"G:\Meine Ablage\UNI\SS26\master_flights_builder\data\reports")
    dst_reports_dir = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie\data\analysis\reports")
    
    if src_reports_dir.exists():
        dst_reports_dir.mkdir(parents=True, exist_ok=True)
        for p in src_reports_dir.iterdir():
            if p.is_file():
                dst_file = dst_reports_dir / p.name
                print(f"Copying report file {p.name} -> {dst_file}")
                shutil.copy2(p, dst_file)
        print("All reports files copied.")
    else:
        print(f"Source reports directory not found at {src_reports_dir}")

if __name__ == "__main__":
    main()
