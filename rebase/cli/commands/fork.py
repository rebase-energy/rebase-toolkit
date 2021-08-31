import click
import os

from ..utils import run_commands

def fork(repo_url, dest_folder: str = "."
) -> None:
    saved_dir = os.getcwd()
    try:    
        os.makedirs(dest_folder, exist_ok=True)
        os.chdir(dest_folder)        
        
        run_commands([f'git clone {repo_url} .',
                      f'dvc pull'],
                     raise_error=True)
    finally:
        os.chdir(saved_dir)

@click.command("fork")
@click.argument("repo_url")
@click.option("--dest-dir", "-d", "dest_folder")
def fork_cmd(*args, **kwargs):
    return fork(*args, **kwargs)

__all__ = ['fork_cmd', 'fork']