import click
import os
import mlflow
import logging
import ast

from ..utils import *
from ..context import current_context, Context, Stage
from ..stage_contexts import *


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
		tags: list =[],
        hyperparam: bool = False
) -> None:
    context = current_context(from_file=True)

    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)

    with repo_chdir(context):
        run_id = generate_run_id()
        run_name = "r-"+run_id[:7]

        try:
            retcode, output = run_command('dvc pull', return_output=True)
        except e:
            print(f"DVC pull failed: {e}")

        params_str = " ".join([f"-S {format_dvc_param(pstr)}" for pstr in parameters]) if parameters else ""
        tags_str = ";".join([format_dvc_tag(tstr) for tstr in tags]) if tags is not None else ""
        if hyperparam:
            print("Using dvc exp run for hyperparam tuning...")
            dvc_cmd = f"dvc exp run -f -n {name} {params_str} \n"
            # TODO: what about the separate branch?
        else:
            dvc_cmd = f"dvc repro \n"

        try:
            tracking_uri = context['envs']['MLFLOW_TRACKING_URI']
            mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_experiment(context['experiment']['name'])

            with open("MLProject", "w") as f:
                f.writelines([f"name: {name}\n",
                              f"entry_points:\n",
                              f"  main:\n",
                              f"    command: python -c 'import mlflow;\
                                                        mlflow.set_tracking_uri(\"{tracking_uri}\");\
                                                        mlflow.set_experiment(\"{context['experiment']['name']}\");\
                                                        mlflow.set_tag(\"mlflow.runName\", \"{name}\");\
                                                        {tags_str}'; "+ dvc_cmd
                             ])
            mrun = mlflow.run(".", use_conda=False)
            mlflow_run_id = mrun.run_id
            logging.info(f"MLFlow run-id: {mlflow_run_id}")
        except Exception as e:
            print(e)
            os.remove("MLProject")
        else:
            os.remove("MLProject")

            print("END rebase run 2")
            retcode, output = run_command(f'dvc commit {name}', return_output=True)
            #if not is_notebook:
            retc, output = run_commands(
                [f'git add .',
                f'git commit -m "{run_name}"'], raise_error=False, return_output=True
            )[-1]
            if retc == 1:
                logging.info("Git - nothing to commit")
            elif retc != 0:
                print("NOT COMMIT run")
                logging.error(f"Git - error commiting changes: {output}")
            else:
                print("COMMITED run")

                retc, output = run_commands([f'dvc push'],
                                             raise_error=False, return_output=True)[-1]
                if retc != 0:
                    logging.error(f"Dvc push failed with output: {output}")


@click.command(name="run")
@click.option("--name", "-n", "name", default=None)
@click.option("--parameter", "-p", "parameters", multiple=True)
@click.option("--tag", "-t", "tags", multiple=True)
@click.option('--hyperparam','-hp', "hyperparam", is_flag=True)
def run_cmd(*args, **kwargs):
	return run(*args, **kwargs)

__all__ = ['run', 'run_cmd']
