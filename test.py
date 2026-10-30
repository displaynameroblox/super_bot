import subprocess
from pathlib import Path
from typing import Union, List

def run_folder_scripts(
    folder_path: Union[str, Path], 
    stop_on_error: bool = True,
    exclude_files: List[str] = None
) -> None:
    """
    Runs all Python files in a directory sequentially.
    
    :param folder_path: Path to the target folder containing .py files.
    :param stop_on_error: If True, stops executing immediately if a script fails.
    :param exclude_files: List of file names to skip.
    """
    script_dir = Path(folder_path)
    if not script_dir.is_dir():
        raise NotADirectoryError(f"The path '{folder_path}' is not a valid directory.")
        
    if exclude_files is None:
        exclude_files = []

    # Gather and alphabetically sort all .py files
    python_files = sorted(script_dir.glob("*.py"))

    for file in python_files:
        if file.name in exclude_files:
            continue
            
        print(f"\n🚀 [Runner] Starting: {file.name}")
        
        try:
            # cwd=script_dir ensures relative file paths within the script work correctly
            subprocess.run(["python3", file.name], cwd=str(script_dir), check=True)
            print(f"✅ [Runner] Finished: {file.name} successfully")
        except subprocess.CalledProcessError as e:
            print(f"❌ [Runner] Error while running {file.name}: {e}")
            if stop_on_error:
                print("🛑 [Runner] Execution halted due to the error.")
                break


run_folder_scripts(
    folder_path="Ai/Tests"
)