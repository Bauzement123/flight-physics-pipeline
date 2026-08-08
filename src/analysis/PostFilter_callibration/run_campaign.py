import subprocess
import logging
from pathlib import Path
import sys
from src.common.utils import setup_file_logger
from src.common.config import BASE_DIR

logger = logging.getLogger(__name__)

def run_stage(stage_module: str, args: list = None):
    cmd = [sys.executable, "-m", stage_module]
    if args:
        cmd.extend(args)
    
    logger.info(f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info(f"Successfully completed {stage_module}")
        # Log the output as debug if needed
        for line in result.stdout.splitlines():
            if line.strip():
                logger.info(f"[{stage_module} STDOUT] {line}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed {stage_module}. Exit code: {e.returncode}")
        for line in e.stdout.splitlines():
            logger.error(f"[{stage_module} STDOUT] {line}")
        for line in e.stderr.splitlines():
            logger.error(f"[{stage_module} STDERR] {line}")
        raise

def main():
    setup_file_logger(log_filename="calibration.log")
    logger.info("=========================================")
    logger.info("Starting Post-Filter Calibration Campaign")
    logger.info("=========================================")
    
    merged_registry_path = BASE_DIR / "data" / "calibration" / "PostFilter_callibration" / "data" / "merged_registry.parquet"
    
    # Define the stages
    stages = [
        ("src.analysis.PostFilter_callibration.stage0_merger", []),
        ("src.analysis.PostFilter_callibration.stage1_directional_impact", ["--input-registry", str(merged_registry_path)]),
        ("src.analysis.PostFilter_callibration.stage2_subspace_validation", []),  # Reads stage1 output
        ("src.analysis.PostFilter_callibration.stage3_undirected_correlation", ["--input-registry", str(merged_registry_path)]),
        ("src.analysis.PostFilter_callibration.stage4_sensitivity_sweep", ["--input-registry", str(merged_registry_path)]),
        ("src.analysis.PostFilter_callibration.stage5_master_sensitivity", []),  # Reads raw master DB
        ("src.analysis.PostFilter_callibration.stage6_prefilter_verification", ["--input-registry", str(merged_registry_path)])
    ]
    
    try:
        for module_path, args in stages:
            logger.info(f"--- Executing {module_path} ---")
            run_stage(module_path, args)
            
        logger.info("=========================================")
        logger.info("Calibration Campaign Completed Successfully")
        logger.info("=========================================")
        print("Campaign completed successfully. All outputs are in data/calibration/PostFilter_callibration/")
        
    except Exception as e:
        logger.error("Campaign failed. See logs for details.")
        sys.exit(1)

if __name__ == "__main__":
    main()
