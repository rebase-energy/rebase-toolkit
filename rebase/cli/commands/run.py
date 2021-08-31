import click
import os
import mlflow
import logging

from ..utils import run_command, generatea_run_id


# template_files = [
#     'src/evaluate.py',
#     'src/featurize.py',
#     'src/prepare.py',
#     'src/train.py',
#     'dvc.yaml',
#     'params.yaml'
# ]


# def copy_template(file_name):
#     data = pkgutil.get_data(__name__, "rebase/template/{}".format(file_name))
#     with open('{}'.format(file_name), 'wb') as f:
#         f.write(data)

# def setup_template():

#     os.system('dvc init')

#     content = {
#         'name': 'test',
#         'entry_points': {
#             'main': {
#                 'command': 'dvc repro'
#             }
#         }
#     }
#     with open('MLproject', 'w') as f:
#         yaml.dump(content, f, sort_keys=False)

#     if not os.path.isdir('src'):
#         os.mkdir('src')

#     for file_name in template_files:
#         copy_template(file_name)

# # def init():
# #     print("Initializing")

# #     setup_template()
# #     print("Created template files in src/")


def format_dvc_param(param_str):
    parts = param_str.split('=')
    value = ast.literal_eval(parts[-1])
    return f"{parts[0]}=\"{value}\""

def format_dvc_tag(tag_str):
    parts = tag_str.split('=')
    return f"mlflow.set_tag(\"{parts[0]}\", \"{parts[-1]}\")"

def run(name: str = None, 
		parameters: list = [], 
		tags: list =[]
) -> None:
    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)
    run_id = generatea_run_id()
    run_name = "exp-"+run_id[:5]

    retcode, output = run_command('dvc pull', return_output=True)
    if retcode != 0:
        raise RuntimeError(f"DVC pull failed: {output}")
    
    params_str = " ".join([f"-S {format_dvc_param(pstr)}" for pstr in parameters]) if parameters else ""
    tags_str = ";".join([format_dvc_tag(tstr) for tstr in tags]) if tags is not None else ""
    try:
        with open("MLProject", "w") as f:
            f.writelines([f"name: {name}\n",
                          f"entry_points:\n",
                          f"  main:\n",
                          f"    command: python -c 'import mlflow;mlflow.set_tag(\"mlflow.runName\", \"{name}\");{tags_str}'; "+
                                f"dvc exp run -n {name} {params_str} \n"
                         ])       
        mrun = mlflow.run(".", use_conda=False)
        mlflow_run_id = mrun.run_id 
        logging.info(f"MLFlow run-id: {mlflow_run_id}")    
    finally:
        os.remove("MLProject")     

@click.command(name="run")
@click.option("--name", "-n", "name", default=None)
@click.option("--parameter", "-p", "parameters", multiple=True)
@click.option("--tag", "-t", "tags", multiple=True)
def run_cmd(*args, **kwargs):
	return run(*args, **kwargs)

__all__ = ['run', 'run_cmd']