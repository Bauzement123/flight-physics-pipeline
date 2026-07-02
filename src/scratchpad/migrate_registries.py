import shutil
from pathlib import Path

def main():
    # 1. Airport coordinates JSON copy
    src_json = Path(r"G:\Meine Ablage\UNI\SS26\master_flights_builder\data\regestries\airport_coordinates.json")
    dst_json = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie\data\registries\airport_coordinates.json")
    
    if src_json.exists():
        dst_json.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_json, dst_json)
        print(f"Copied airport_coordinates.json to {dst_json}")
    else:
        print(f"Warning: Source airport_coordinates.json not found at {src_json}")
        
    # 2. Registries parquet files move
    src_reg_dir = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie\data\flight_registry\registries")
    dst_reg_dir = Path(r"G:\Meine Ablage\UNI\SS26\PythonPipeline - Kopie\data\registries")
    
    if src_reg_dir.exists():
        dst_reg_dir.mkdir(parents=True, exist_ok=True)
        for p in src_reg_dir.glob("*.parquet"):
            dst_file = dst_reg_dir / p.name
            print(f"Moving registry file {p.name} -> {dst_file}")
            shutil.move(str(p), str(dst_file))
        print("All registry parquets migrated.")
    else:
        print(f"No local registries folder found at {src_reg_dir}. Skipping.")

if __name__ == "__main__":
    main()
