import mlflow
import os
from os.path import isfile, isdir, exists
import cloudpickle
import logging
from shutil import copyfile
from contextlib import contextmanager
import sys
import yaml

from .utils import run_commands,run_command, generate_run_id, dict_to_yaml_file, yaml_to_dict,is_notebook
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

@contextmanager
def stage(name, params=None, log_run=False, ctx_run_name=None):
    context = current_context(from_file=True)
    prev_stage = context.current_stage()
    context.set_stage(name, params=params)
    print("INSIDE stage ctx")
    try:
        stage = context.current_stage()
        stage.clear_dependencies()

        logging.info("Restoring context...")
        with repo_chdir():
            if params is None:
                with open("params.yaml", "r") as stream:
                    try:
                        stage.params = yaml.safe_load(stream)[name]
                    except yaml.YAMLError as e:
                        print(e)

            curr_branch = run_command("git rev-parse --abbrev-ref HEAD",
                                  return_output=True)[1].strip()
            if ctx_run_name is not None:

                ctx_commit = run_command(f"git log --oneline --grep='{ctx_run_name}'",
                                      return_output=True)[1].strip().split()[0]
                retc, output = run_commands([f'git checkout {ctx_commit}'], raise_error=False, return_output=True)[0]

            retc, output = run_commands([f'dvc checkout'], raise_error=False, return_output=True)[0]
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
        run_name = f"r-{generate_run_id()[:7]}"
        if log_run:
            mlflow.autolog()
            run_obj = mlflow.active_run()
            if run_obj is None:
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

            # track outputs with dvc
            """
            print("ADDING OUTPUTS WITH DVC")
            for k,d in stage.outputs.items():
                retc, output = run_commands([f"dvc add {d['path']}"], return_output=True)[0]
                if retc != 0:
                    print(f"dvc add error for outputs: {output}")
                else:
                    print(f"dvc added {d['path']}")
            """

            # get previous dvc pipeline from yaml file
            print("GENERATE pipeline")
            if exists('dvc.yaml'):
                with open("dvc.yaml", "r") as stream:
                    try:
                        prev_dvc_pipeline = yaml.safe_load(stream)
                    except yaml.YAMLError as e:
                        prev_dvc_pipeline = None
            else:
                prev_dvc_pipeline = None

            # TODO: generate with dvc run --no-exec instead to make the outputs tracked
            dvc_pipeline = context.generate_dvc_pipeline(prev_dvc_pipeline)
            dict_to_yaml_file(dvc_pipeline, "dvc.yaml")

            if params:
                dict_to_yaml_file(params, "params.yaml")

            # if notebook then git commit and dvc push
            if is_notebook:
                print("INSIDE notebook")

                retc, output = run_commands(
                    [f'git add .',
                    f'git commit -m "{run_name}"'], raise_error=False, return_output=True
                )[-1]
                if retc == 1:
                    logging.info("Git - nothing to commit")
                elif retc != 0:
                    print("NOT COMMIT notebook")
                    logging.error(f"Git - error commiting changes: {output}")
                else:
                    print("COMMITED notebook")
                    retc, output = run_commands([f'dvc push'],
                                                 raise_error=False, return_output=True)[-1]
                    if retc != 0:
                        logging.error(f"Dvc push failed with output: {output}")
            else:
                print("NOT INSIDE notebook")
    finally:
        logging.info("Run - ending ...")
        print("ENDING RUN")
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
        print("END RUN")

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
