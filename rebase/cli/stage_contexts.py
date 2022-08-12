import mlflow
import os
from os.path import isfile, isdir, exists
import cloudpickle
import logging
from shutil import copyfile
from contextlib import contextmanager
import sys
import yaml

from .utils import (run_commands,run_command, 
                    generate_run_id, 
                    dict_to_yaml_file, 
                    yaml_to_dict,
                    is_notebook,
                    git_get_current_branch)
from .context import current_context, Context, Stage
from .errors import *
from ..api.sdk import project_init

@contextmanager
def repo_chdir(context=None):
    """
    Ctx manager to perform commands in the directory of the data repo.
    """
    if context is None:
        context = current_context()
    saved_dir = os.getcwd()
    repo_dir = context["path"]
    try:
        os.chdir(repo_dir)
        yield repo_dir
    finally:
        os.chdir(saved_dir)

def merge_collection_args(stage_name, collection, stored_collection_name, stored_dvc, arg):
    args_list = []
    if stored_dvc is not None and stage_name in stored_dvc['stages']:
        stored_stage = stored_dvc['stages'][stage_name]        
        args_list = stored_stage.get(stored_collection_name, [])
        if args_list is None:
            args_list = []
    else:
        args_list = []

    for c in collection:
        if c not in args_list:
            args_list.append(c)

    return "".join(f"{arg}{v}" for v in args_list)


@contextmanager
def stage(name, params=None, log_run=False, ctx_run_name=None):
    context = current_context(from_file=True)
    prev_stage = context.current_stage()
    context.set_stage(name, params=params)
    print("INSIDE stage ctx")
    try:
        stage = context.current_stage()
        #stage.clear_dependencies()

        logging.info("Restoring context...")
        with repo_chdir():
            if exists('params.yaml'):
                try:
                    with open('params.yaml', 'r') as stream:
                        all_params = yaml.safe_load(stream)
                except yaml.YAMLError as e:
                    print(e)
            else:
                all_params = { name: {} }

            if params is not None:
                all_params[name].update(params)
            if name not in all_params:
                all_params[name] = {}

            stage.params = all_params[name]

            curr_branch = git_get_current_branch()
            if ctx_run_name is not None:

                ctx_commit = run_command(f"git log --oneline --grep='{ctx_run_name}'",
                                      return_output=True)[1].strip().split()[0]
                retc, output = run_commands([f'git checkout {ctx_commit}'], raise_error=False, return_output=True)[0]

            # retc, output = run_commands([f'dvc checkout'], raise_error=False, return_output=True)[0]
        """
            if os.path.isfile('dvc.yaml'):
                dvc_yaml = yaml_to_dict('dvc.yaml')
                if 'stages' in dvc_yaml and name in dvc_yaml['stages']:
                    retc, output = run_commands([f'dvc pull {name}'], raise_error=False, return_output=True)[0]
                    if retc == 0:
                        logging.error(f"Dvc - pulled deps from '{name}' : {output}")
                    else:
                        logging.error(f"Dvc - error pulling from '{name}' : {output}")
        """        
        if log_run:
            mlflow.autolog()
            run_obj = mlflow.active_run()
            if run_obj is None:
                run_name = f"r-{generate_run_id()[:7]}"
                run_obj = mlflow.start_run(
                    run_name=run_name,
                    experiment_id=context['experiment']['id']
                )
            else:
                run_name = run_obj.data.tags['mlflow.runName']

            mlflow_run_id = run_obj.info.run_id
            mlflow_artifact_uri = run_obj.info.artifact_uri
            context.set_current_run(mlflow_run_id) # TODO: change method name
            context.artifact_uri = mlflow_artifact_uri

        yield stage

        with repo_chdir(context):
            # add dvc files for outputs
            #for k, d in stage.outputs.items():
            #    out_path = d['path']
            #    full_out_path = os.path.join(context['path'], out_path)
            #    run_commands([f'dvc add {full_out_path}'])

            context = current_context()
            stage = context.current_stage()
            #context.set_stage(name, params=params)
            #context.stages[name] = stage
          
            # get previous dvc pipeline from yaml file
            logging.info("GENERATE pipeline")
            if exists('dvc.yaml'):
                with open("dvc.yaml", "r") as stream:
                    try:
                        prev_dvc_pipeline = yaml.safe_load(stream)
                    except yaml.YAMLError as e:
                        prev_dvc_pipeline = None
            else:
                prev_dvc_pipeline = None

            deps_arg = merge_collection_args(name, [d['path'] for d in stage.dependencies.values()], "deps", prev_dvc_pipeline, " -d ")
            outs_arg = merge_collection_args(name, [o['path'] for o in stage.outputs.values()], "outs", prev_dvc_pipeline, " -o ")        
            if stage.params is not None and len(stage.params.keys()) > 0:
                params_arg = "-p " + merge_collection_args(name, [f'{name}.{p}' for p in stage.params.keys()], "params", prev_dvc_pipeline, ",")
            else:
                params_arg = ""
            if prev_dvc_pipeline is not None and name in prev_dvc_pipeline['stages']:
                command_arg = prev_dvc_pipeline['stages'][name].get('cmd', "")
            else:
                command_arg = f"echo \"python yourpath/{name}.py\""
            retc, output = run_commands([f"dvc run -f -n {name} {deps_arg} {outs_arg} {params_arg} --no-exec '{command_arg}'"], return_output=True)[0]
            if retc != 0:
                logging.error(f"dvc run error for outputs: {output}")            

            # TODO: generate with dvc run --no-exec instead to make the outputs tracked
            # dvc_pipeline = context.generate_dvc_pipeline(prev_dvc_pipeline)
            # dict_to_yaml_file(dvc_pipeline, "dvc.yaml")

            dict_to_yaml_file(all_params, "params.yaml")

            # # if notebook then git commit and dvc push
            # if is_notebook:
            #     print("INSIDE notebook")

            #     retc, output = run_commands(
            #         [f'git add .',
            #         f'git commit -m "{run_name}"'], raise_error=False, return_output=True
            #     )[-1]
            #     if retc == 1:
            #         logging.info("Git - nothing to commit")
            #     elif retc != 0:
            #         print("NOT COMMIT notebook")
            #         logging.error(f"Git - error commiting changes: {output}")
            #     else:
            #         print("COMMITED notebook")
            #         retc, output = run_commands([f'dvc push'],
            #                                      raise_error=False, return_output=True)[-1]
            #         if retc != 0:
            #             logging.error(f"Dvc push failed with output: {output}")
            # else:
            #     print("NOT INSIDE notebook")
    finally:
        logging.info("Run - ending ...")
        if log_run:
            mlflow.end_run()
            context.clear_run()
            context.artifact_uri = None

        if ctx_run_name is not None:
            # todo: when not notebook???
            logging.info("Git - restoring to current branch")
            with repo_chdir(context):
                retl = run_commands([f'git checkout -B tmp_rb',
                              f'git checkout {curr_branch}',
                              f'git rebase tmp_rb',
                              f'dvc checkout',
                              f'git branch -D tmp_rb'],raise_error=False, return_output=True)
                for (retc, output) in retl:
                    if retc != 0:
                        logging.error(f"Git/DVC - error in restoring to current branch: {output}")

        context.set_stage(prev_stage.name, prev_stage.params)
        context.save()
        logging.info("Context saved")        

@contextmanager
def mlflow_ctx(experiment):
    """
    Ctx manager to perform commands with mlflow for an experiment
    """
    try:
        existing_experiment = mlflow.get_experiment_by_name(experiment)
        if existing_experiment is None:
            raise ValueError("Experiment doesn't exist")
        else:
            experiment_id = existing_experiment.experiment_id
        yield experiment_id
    finally:
        pass


__all__ = ['stage','repo_chdir','mlflow_ctx']
