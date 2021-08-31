import click
import mlflow
import os
import cloudpickle
import logging
from shutil import copyfile
from contextlib import contextmanager

from ..utils import run_commands, generatea_run_id, dict_to_yaml_file
from ..context import current_context
from ..errors import *
from ...api.sdk import project_init

def init(project: str = None, 
         experiment: str = None,         
         project_dir: str = None,
         context: dict = None,
         artifact_location: str = None
) -> None:
    print("Initializing")
    if context is None:
        context = current_context()

    if project is None:
        raise ValueError("Missing project name")

    proj_config = project_init(project)

    print(proj_config)
    for k, v in proj_config['envs'].items():
        os.environ[k] = v

    current_dir = os.getcwd()
    if project_dir is None:
        project_dir = f"{current_dir}/{project}"
    existing_experiment = mlflow.get_experiment_by_name(experiment)
    if existing_experiment is None:
        experiment_id = mlflow.create_experiment(experiment, artifact_location=artifact_location or proj_config['artifact_location'])
    else:
        experiment_id = existing_experiment.experiment_id
    context['project'] = project
    context['config'] = proj_config
    context['experiment'] = { 'name': experiment,
                              'id': experiment_id}    
    context['path'] = project_dir
    
    if not os.path.exists(project_dir):
        init_proj_dir(project_dir)


def init_proj_dir(project_dir):
    context = current_context()
    os.makedirs(project_dir, exist_ok=True)

    proj_config = context["config"]
    with repo_chdir():
        run_commands([f"git init",
                      f"dvc init",
                      f"dvc remote add --default rebase {proj_config['data_location']}"])

@contextmanager
def repo_chdir():
    context = current_context()
    saved_dir = os.getcwd()
    try:
        os.chdir(context["path"])
        yield
    finally:
        os.chdir(saved_dir)


def add_dependency(path: str, name: str = None, externals: str = "ref_direct"):
    context = current_context()
    stage = context.current_stage()

    if name is None:
        name = path
        
    file_path_abs = os.path.abspath(path)
    dvc_add_flags = ""
    copy_file = False

    if not os.path.samefile(os.path.commonpath([
                                file_path_abs, 
                                os.path.abspath(context['path']),
                            ]), os.path.abspath(context['path'])):
        logging.info(f"Dependency {path} is outside the repo. Action: {externals}")
        dir_name = os.path.dirname(file_path_abs)
        remote_name = os.path.basename(dir_name)

        if externals == "ref_direct":
            with repo_chdir():       
    #             output = run_commands([f"bash -c \"echo $(dvc remote list | awk \'{{print $1}}\')\""], return_output=True)[0][1]
    #             logging.info(output.split("\n"))
    #             remote_exists = remote_name in output.split("\n")
    # #            logging.info(f"bash -c 'dvc remote list output | awk \'{{print $1}}\' | grep {remote_name}'")
    #             if not remote_exists:
    #                 run_commands([f"dvc remote add {remote_name} {dir_name}"])
    #             else:
    #                 logging.info(f"Remote {remote_name} already exists.")
                #dep_file = f"/{remote_name}/{file_rel_to_remote}"
                dep_file = file_path_abs
                dvc_add_flags = "--external"
        elif externals == "copy":            
            dep_file = name
            copy_file = True
        elif externals == "raise":
            raise ExternalsDisabledError()
    else:        
        dep_file = os.path.relpath(file_path_abs, context['path'])        
    
    if stage.get_dependency_by_path(dep_file) is None:        
        with repo_chdir():       
            if copy_file:
                dest_file = os.path.join(context['path'], dep_file)
                file_dir = os.path.dirname(dest_file)
                os.makedirs(file_dir, exist_ok=True)
                copyfile(file_path_abs, dest_file)

            run_commands([f"dvc add {dvc_add_flags} {dep_file}"])

        stage.add_dependency(dep_file, name=name)
    else:
        logging.info(f"Depencency {dep_file} already exists")

    return dep_file

# def dep(path: str) -> str:
#     context = current_context()
#     stage = context.current_stage()
#     return stage.get_dependency_by_path(name)

@contextmanager
def stage(name, params=None, log_run=False):
    context = current_context()
    prev_stage = context.current_stage()
    context.set_stage(name, params=params)
    try:
        stage = context.current_stage()
        stage.clear_dependencies()
        run_name = f"r-{generatea_run_id()[:5]}"
        if log_run:            
            mlflow.autolog()
            mlflow.start_run(run_name=run_name, experiment_id=context['experiment']['id'])    

        yield stage

        with repo_chdir(): 
            dvc_pipeline = context.generate_dvc_pipeline()
            dict_to_yaml_file(dvc_pipeline, "dvc.yaml")
            if params:
                dict_to_yaml_file(params, "params.yaml")

            retc, output = run_commands([f'dvc commit -f'], raise_error=False, return_output=True)[0]
            if retc == 0:
                retc, output = run_commands([f'git add .',
                                             f'git commit -m "run_name:{run_name}"'], raise_error=False, return_output=True)[-1]
                if retc == 1:
                    logging.info("Git - nothing to commit")
                elif retc != 0:
                    logging.error(f"Git - error commiting changes: {output}")
                else:
                    retc, output = run_commands([f'dvc push'], raise_error=False, return_output=True)[-1]
                    if retc != 0:
                        logging.error(f"Dvc push failed with output: {output}")
            else:
                logging.error(f"Dvc - error commiting changes: {output}")
    finally:
        if log_run:
            mlflow.end_run()
        context.set_stage(prev_stage.name, prev_stage.params)


def load_pickle(path, name=None):
    context = current_context()
    stage = context.current_stage()    
    file_path = os.path.join(context['path'], path)
    stage.add_dependency(path, name=name)
    with open(file_path, "rb") as f:
        return cloudpickle.load(f)

def log_model(model, name=None):
    file_path = save_pickle(model, name, name=name)
    mlflow.log_artifact(file_path)

def save_pickle(obj, path, name=None): 
    context = current_context()
    stage = context.current_stage()
    rel_dest_file = os.path.join(context['path'], path)
    file_dir = os.path.dirname(rel_dest_file)
    os.makedirs(file_dir, exist_ok=True)
    with open(rel_dest_file, "wb") as f:
        cloudpickle.dump(obj, f)
    # with repo_chdir():
    #     run_commands([f"dvc add {rel_dest_file}"])
    stage.add_output(path, name=name)

    return rel_dest_file
    # temp_file_fd, temp_filename = tempfile.mkstemp()
    # dest_file = os.path.join(context['data_folder'], name)
    # try:      
    #   with open(dest_file) as f:
    #       cloudpickle.dump(obj, f)
        
    #   fhasn = file_hash(temp_filename)
    #   dest_file = os.path.join(context['data_folder'], f"{fhasn}.pkl")
    #   if not os.path.exists(dest_file):
    #       copyfile(temp_filename, dest_file)
    # finally:
    #   os.close(temp_file_fd)



@click.command(name="init")
@click.option("--project", "-p", "project")
@click.option("--experiment", "-e", "experiment")
def init_cmd(*args, **kwargs):
    return init(*args, **kwargs)

__all__ = ['init', 'init_cmd', 'stage', 'add_dependency', 'load_pickle', 'save_pickle', 'log_model']