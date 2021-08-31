import subprocess
import logging
import sys
import uuid
import hashlib
import yaml
import shlex

def generatea_run_id():
    return str(uuid.uuid4()).replace('-', '')

def run_command(command, return_output=False):
    command_list = shlex.split(command)
           
    try:
        logging.info("Running: \"{}\"".format(command))
        result = subprocess.run(command_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE);
        return result.returncode, result.stdout.decode('UTF-8')+result.stderr.decode('UTF-8') if return_output else None
    except Exception as e:
        logging.error("Exception: {}".format(e))
        return -1, None

def run_commands(commands, stop_on_failure=True, return_output=False, raise_error=True):
    results = []
    for cmd in commands:
        retcode, result = run_command(cmd, return_output=True)
        if not return_output:
            result = None    
        results.append((retcode, result))
        if stop_on_failure and retcode != 0:
            if raise_error:
                raise RuntimeError("Error executing command %s: %d, result: %s\n\nResults from commands ran so far: %s" % (cmd, retcode, result, results))
            else:
                return results
    return results

def file_hash(file):
    if isinstance(file, str):
        with open(file_path, 'rb') as f:
            return file_hash(f)

    # ref: https://nitratine.net/blog/post/how-to-hash-files-in-python/
    BLOCK_SIZE = 65536 # The size of each read from the file
    file_hash = hashlib.sha256() # Create the hash object, can use something other than `.sha256()` if you wish
    fb = f.read(BLOCK_SIZE) # Read from the file. Take in the amount declared above
    while len(fb) > 0: # While there is still data being read from the file
        file_hash.update(fb) # Update the hash
        fb = f.read(BLOCK_SIZE) # Read the next block from the file

    return file_hash.hexdigest() # Get the hexadecimal digest of the hash

def dict_to_yaml_file(d: dict, file_path: str):
    with open(file_path, 'w') as outfile:
        yaml.dump(d, outfile, default_flow_style=False)  