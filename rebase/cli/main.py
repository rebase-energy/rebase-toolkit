import argparse
import yaml
import pkgutil
import os, sys
import ast
import mlflow
import json
import rebase.util.api_request as api_request
import rebase as rb
import subprocess
import logging
import uuid
import re
import tempfile

logging.basicConfig(format='%(asctime)s:%(name)s:%(levelname)s:%(message)s', level=logging.INFO, stream=sys.stdout)

template_files = [
    'src/evaluate.py',
    'src/featurize.py',
    'src/prepare.py',
    'src/train.py',
    'dvc.yaml',
    'params.yaml'
]


def copy_template(file_name):
    data = pkgutil.get_data(__name__, "rebase/template/{}".format(file_name))
    with open('{}'.format(file_name), 'wb') as f:
        f.write(data)

def setup_template():

    os.system('dvc init')

    content = {
        'name': 'test',
        'entry_points': {
            'main': {
                'command': 'dvc repro'
            }
        }
    }
    with open('MLproject', 'w') as f:
        yaml.dump(content, f, sort_keys=False)

    if not os.path.isdir('src'):
        os.mkdir('src')

    for file_name in template_files:
        copy_template(file_name)

def run_command(command, return_output=False):
    command_list = command.split(' ')
           
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
        retcode, result = run_command(cmd, return_output=return_output)
        if stop_on_failure and retcode != 0:
            if raise_error:
                raise RuntimeError("Error executing command %s: %d, result: %s\n\nResults from commands ran so far: %s" % (cmd, retcode, result, results))
            else:
                return results
        results.append((retcode, result))
    return results

def init():
    print("Initializing")

    setup_template()
    print("Created template files in src/")

def format_dvc_param(param_str):
    parts = param_str.split('=')
    value = ast.literal_eval(parts[-1])
    return f"{parts[0]}=\"{value}\""

def format_dvc_tag(tag_str):
    parts = tag_str.split('=')
    return f"mlflow.set_tag(\"{parts[0]}\", \"{parts[-1]}\")"

def update_params_file(param_list):
    if param_list:
        with open('params.yaml', 'r') as f:
            params = yaml.safe_load(f)
            # Recursively merges params likes this:
            # train.learning_rate=0.2
            # into this:
            # {..., 'train': {'learning_rate': 0.2, ...}, ...}
            def update(d, keys, v):
                k = keys[0]
                if len(keys) == 1:
                    d[k] = v
                    return d
                return {**d, k: update(d[k], keys[1:], v)}

            for p in param_list:
                parts = p.split('=')
                value = ast.literal_eval(parts[-1])
                keys = parts[0].split('.')
                params = update(params, keys, value)

        with open('params.yaml', 'w') as f:
            yaml.dump(params, f, sort_keys=False)


def generatea_run_id():
    return str(uuid.uuid4()).replace('-', '')

def run(run_args):    
    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)
    run_id = generatea_run_id()
    run_name = "exp-"+run_id[:5]

    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--name', default=run_name)
    parser.add_argument('-p', action='append')
    parser.add_argument('-t', action='append')   
    args = parser.parse_args(run_args)

    retcode, output = run_command('dvc pull', return_output=True)
    if retcode != 0:
        raise RuntimeError(f"DVC pull failed: {output}")
    
    params_str = " ".join([f"-S {format_dvc_param(pstr)}" for pstr in args.p])
    tags_str = ";".join([format_dvc_tag(tstr) for tstr in args.t]) if args.t is not None else ""
    try:
        with open("MLProject", "w") as f:
            f.writelines([f"name: {args.name}\n",
                          f"entry_points:\n",
                          f"  main:\n",
                          f"    command: python -c 'import mlflow;mlflow.set_tag(\"mlflow.runName\", \"{args.name}\");{tags_str}'; "+
                                f"dvc exp run -n {args.name} {params_str} \n"
                         ])       
        mrun = mlflow.run(".", use_conda=False)
        mlflow_run_id = mrun.run_id 
        logging.info(f"MLFlow run-id: {mlflow_run_id}")    
    finally:
        os.remove("MLProject")        


def hpsearch(cmd_args):
    print("Starting hyperparam search")
    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)
    run_id = generatea_run_id()
    run_name = f"hps-{run_id[:5]}"
    run_command(f"git commit -a -m 'HP:{run_name}'")
    retc, output = run_command("git config --get remote.origin.url", return_output=True)
    if retc != 0:
        raise RuntimeError(f"Could not extract git repo url: {output}")
    git_remote_url = output.strip()

    with open('hyperparams.yaml', 'r') as f:
        hyperparams = yaml.safe_load(f)
        data = {
            'hyperparams': hyperparams,
            'git_remote_url': git_remote_url,
            'repo_name': repo_name,
            'run_name': run_name,
            'api_key': rb.api_key
        }

    r = api_request.post('platform/v1/model/hpsearch', data=json.dumps(data))
    print("Status", r.status_code)
    data = r.json()
    print('hpsearch id: {}'.format(data['hp_id']))


def fork(fork_args=""):
    parser = argparse.ArgumentParser()
    parser.add_argument('repo_url')
    parser.add_argument('dest_folder', default=".")
    args = parser.parse_args(fork_args)

    saved_dir = os.getcwd()
    try:    
        os.makedirs(args.dest_folder, exist_ok=True)
        os.chdir(args.dest_folder)        
        
        run_commands([f'git clone {args.repo_url} .',
                      f'dvc pull'],
                     raise_error=False)
    finally:
        os.chdir(saved_dir)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command')
    parser.add_argument('remaining_args', default="", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    try:
        command_func = globals()[args.command]
    except KeyError:
        print(f"Invalid command {args.command}")
    else:
        command_func(args.remaining_args)

if __name__ == "__main__":
    main()