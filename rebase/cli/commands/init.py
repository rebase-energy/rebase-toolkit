import click
import mlflow
import os
import cloudpickle
import logging
from shutil import copyfile
from contextlib import contextmanager

from ..utils import run_commands,run_command, generate_run_id, dict_to_yaml_file, yaml_to_dict
from ..context import current_context
from ..errors import *
from ...api.sdk import project_init

def init(project: str = None,
         experiment: str = None,
         project_dir: str = None,
         context: dict = None,
         artifact_location: str = None
) -> None:
    print("Initializing....")

    # get internal rebase context
    if context is None:
        context = current_context()

    # setup rebase project
    if project is None:
        raise ValueError("Missing project name")

    proj_config = project_init(project)
    print(f"Project config: \n{proj_config}")
    for k, v in proj_config['envs'].items():
        os.environ[k] = v

    # setup mlflow experiment
    print("Setting up environments...")
    current_dir = os.getcwd()
    if project_dir is None:
        project_dir = f"{current_dir}/{project}"
    existing_experiment = mlflow.get_experiment_by_name(experiment)
    if existing_experiment is None:
        experiment_id = mlflow.create_experiment(experiment, artifact_location=artifact_location or proj_config['artifact_location'])
    else:
        experiment_id = existing_experiment.experiment_id

    # set info in rebase internal context
    context['project'] = project
    context['config'] = proj_config
    context['experiment'] = { 'name': experiment,
                              'id': experiment_id}
    context['path'] = project_dir

    init_proj_dir(project_dir)

def init_proj_dir(project_dir):
    context = current_context()
    os.makedirs(project_dir, exist_ok=True)

    proj_config = context["config"]

    print("Init project dir...")
    with repo_chdir() as repo_dir:
        # check if inside git repo
        _, git_repo = run_command("git rev-parse --is-inside-work-tree",
                                  return_output=True)

        if not 'true' in git_repo.lower():
            raise OSError("Not inside git repo...")

        dvc_inited = os.path.isdir(".dvc")

        if not dvc_inited:
        # check if dvc repo
            run_commands([#f"git init",
                          f"dvc init --subdir",
                          #f"git add .dvc*",
                          #f"git commit -m 'dvc init '",
                          f"dvc remote add --default rebase {proj_config['data_location']}"])

            # commit .dvc files if new repo
            run_commands([f"git add .dvc*",
                          f"git commit -m 'dvc init {project_dir}'"])

        user_email = os.environ.get('GIT_EMAIL')
        user_name = os.environ.get('GIT_USERNAME')
        if user_email is None or user_name is None:
            user_email = "RbUser@rebase.energy"
            user_name = "RbUser"

        run_commands([f"git config user.email \"{user_email}\"",
                      f"git config user.name \"{user_name}\""])
        print("git and dvc initialised...")

@contextmanager
def repo_chdir():
    """
    Ctx manager to perform commands in the directory of the data repo.
    """
    context = current_context()
    saved_dir = os.getcwd()
    repo_dir = context["path"]
    try:
        os.chdir(repo_dir)
        yield repo_dir
    finally:
        os.chdir(saved_dir)

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


def add_dependency(location: str, out: str = None, externals: str = "ref_direct",
                    repo: str = None, remote: bool = False, revision: str = None):
    """
    TODO: Add some documentation, this function does a lot
    """
    # dvc import https://github.com/worldyn/rebase-test.git price-forecast/ins/test.csv --rev v15 -o minfil.csv
    context = current_context()
    stage = context.current_stage()

    # deconstruct path???
    # dvc import https://github.com/worldyn/rebase-test.git price-forecast/outs/train.pkl --rev v1 --out outs/train.pkl

    dvc_add_flags = ""
    copy_file = False

    if out is None and not remote:
        out = location
    elif out is None and remote:
        out = "."

    if repo is not None:
        remote = True

    if remote:
        # TODO: what for ref_direct and raise???
        dep_file = out # external path, e.g url
    else:
        # fix correct file paths
        file_path_abs = os.path.abspath(location)
        if not os.path.samefile(os.path.commonpath([
                                    file_path_abs,
                                    os.path.abspath(context['path']),
                                ]), os.path.abspath(context['path'])):
            logging.info(f"Dependency {location} is outside the repo. Action: {externals}")
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
                dep_file = out
                copy_file = True
            elif externals == "raise":
                raise ExternalsDisabledError()
        else:
            dep_file = os.path.relpath(file_path_abs, context['path'])

    if stage.get_dependency_by_path(dep_file) is None:
        with repo_chdir():
            # create output dir if it doesn't exist
            if copy_file or remote:
                dest_file = os.path.join(context['path'], dep_file)
                file_dir = os.path.dirname(dest_file)
                os.makedirs(file_dir, exist_ok=True)

            if copy_file and not remote:
                copyfile(file_path_abs, dest_file)

            # add the dependency
            if not remote:
                retc, output = run_commands([f"dvc add {dvc_add_flags} {dep_file}"], return_output=True)
                if retc != 0:
                    print(f"dvc add error: {output}")
            else:
                try:
                    if repo is not None:
                        rev_str = f"--rev {revision}" if revision is not None else ""

                        run_commands([f"dvc import {dvc_add_flags} {rev_str} {repo} {location} -o {dep_file}"])
                    else:
                        run_commands([f"dvc import-url {dvc_add_flags} {location} {dep_file}"])
                except:
                    if not os.path.exists(out):
                        raise OSError("Dvc import failed...")
            dep_file_abs = os.path.abspath(dep_file)
        stage.add_dependency(dep_file, name=out)

        return dep_file_abs
    else:
        logging.info(f"Depencency {dep_file} already exists")

    return dep_file

# def dep(path: str) -> str:
#     context = current_context()
#     stage = context.current_stage()
#     return stage.get_dependency_by_path(name)

@contextmanager
def stage(name, params=None, log_run=False, ctx_run_name=None):
    context = current_context()
    prev_stage = context.current_stage()
    context.set_stage(name, params=params)
    try:
        stage = context.current_stage()
        stage.clear_dependencies()

        logging.info("Restoring context...")
        with repo_chdir():
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
            run_obj = mlflow.start_run(
                run_name=run_name,
                experiment_id=context['experiment']['id']
            )
            mlflow_run_id = run_obj.info.run_id
            mlflow_artifact_uri = run_obj.info.artifact_uri
            context.set_current_run(mlflow_run_id) # TODO: change method name
            context.artifact_uri = mlflow_artifact_uri

        yield stage

        with repo_chdir():
            # add dvc files for outputs
            #for k, d in stage.outputs.items():
            #    out_path = d['path']
            #    full_out_path = os.path.join(context['path'], out_path)
            #    run_commands([f'dvc add {full_out_path}'])

            dvc_pipeline = context.generate_dvc_pipeline()
            dict_to_yaml_file(dvc_pipeline, "dvc.yaml")
            if params:
                dict_to_yaml_file(params, "params.yaml")

            deps = ""
            outs = ""
            for dep in dvc_pipeline['stages'][name]['deps']:
                deps += f"-d {dep} "
            for out in dvc_pipeline['stages'][name]['outs']:
                outs += f"-o {out} "

            retc, output = run_commands([f'dvc run -n {name} {deps} {outs} --no-exec --force echo \"{name}.py not implemented\"',
                                         f'dvc commit -f'],
                                         raise_error=False,
                                         return_output=True)[0]
            if retc == 0:
                retc, output = run_commands([f'git add .',
                                             f'git commit -m "{run_name}"'], raise_error=False, return_output=True)[-1]
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
            context.clear_run()
            context.artifact_uri = None

        if ctx_run_name is not None:
            logging.info("Git - restoring to current branch")
            with repo_chdir():
                retl = run_commands([f'git checkout -B tmp_rb',
                              f'git checkout {curr_branch}',
                              f'git rebase tmp_rb',
                              f'dvc checkout',
                              f'git branch -D tmp_rb'],raise_error=False, return_output=True)
                for (retc, output) in retl:
                    if retc != 0:
                        logging.error(f"Git/DVC - error in restoring to current branch: {output}")

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
    return file_path

def publish_model(name: str, run_name: str = None):
    """
    Publish model artifact into model registry
    Appends version if already existing.
    """
    if not isinstance(name, str):
        raise ValueError("Name is required to be a string")

    context = current_context()
    curr_experiment = context['experiment']['name']
    with mlflow_ctx(curr_experiment) as experiment_id:
        if run_name is None:
            mlflow_run_id = context.get_run()
            artifact_uri = context.artifact_uri
        else:
            run = mlflow_run_from_name(run_name, experiment_id)
            mlflow_run_id = run.info.run_id
            artifact_uri = run.info.artifact_uri

        #print("MLFLOW RUN: ", mlflow_run_id)
        #print("ARTIFCAT URI: ", artifact_uri)
        #context['experiment']['id']

        model_uri = "runs:/{}/{}".format(mlflow_run_id, artifact_uri)
        mlflow.register_model(model_uri, name)

def load_model(run_name, repo = None, model_uri=None):
    """
    Get loaded model artifact from run name
    If repo is not set then assumes model is
    fetched from run in current mlflow experiment.
    """

    ### TODO: get from repo

    if not isinstance(run_name, str):
        raise ValueError("'run_name' is required to be a string")

    if model_uri is None:
        context = current_context()
        run = mlflow_run_from_name(run_name)

        run_id = run.info.run_id
        model_uri = f"runs:/{run_id}/model"

    model = mlflow.pyfunc.load_model(model_uri)
    flavors = list(model.metadata.flavors.keys())
    return model

def mlflow_run_from_name(run_name, experiment_id = None):
    """
    Returns: mlflow.entities.Run
    """
    if not isinstance(run_name, str):
        raise ValueError("'run_name' is required to be a string")

    if experiment_id is None:
        context = current_context()
        experiment_id = context['experiment']['id']

    runs_list = mlflow.search_runs(
        experiment_ids=[experiment_id],
        filter_string=f'tags.mlflow.runName = "{run_name}"',
        output_format = "list"
    )
    if len(runs_list) == 0:
        raise ValueError(f"No runs found for name '{run_name}'")
    if len(runs_list) > 1:
        raise ValueError(f"Multiple runs found for name '{run_name}'")
    return runs_list[0]

def save_pickle(obj, path, name=None):
    context = current_context()
    stage = context.current_stage()

    rel_dest_file = os.path.join(context['path'], path)
    file_dir = os.path.dirname(rel_dest_file)
    os.makedirs(file_dir, exist_ok=True)
    with open(rel_dest_file, "wb") as f:
        cloudpickle.dump(obj, f)

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

def restore(stage: str):
    with repo_chdir():
        _, out = run_command(f'dvc pull {stage}',return_output=True)
        print(out)

def log_param(key: str ,value: any):
    mlflow.log_param(key, value)

def log_metric(key: str ,value: float, step: int =None):
    mlflow.log_metric(key, value, step)

def log_params(params_dict):
    mlflow.log_params(params_dict)

def log_metrics(metrics_dict, step: int = None):
    mlflow.log_metrics(metrics_dict, step)

def list(experiment: str, key: str = None, type: str = "metrics",
        return_runs: bool = False, max_results = 10):
    """
    Prints runs for an experiment, and returns list of IDs
    Can be ordered by a metric or param, saved in the runs.

    type: "metric" or "param"
    :param str experiment_id: id of experiment (mlflow)
    :param str key: name of param/metric to sort runs by
    :param str type: type of key, either 'metric' or 'param'
    :param return_runs: decide if to return a dictionary with runs
    :param max_results: pagination max number
    :return: if return_runs = True then dict: run_id => mlflow run object
    """
    with mlflow_ctx(experiment) as experiment_id:
        if type != "metrics" and type != "params":
            raise ValueError("Type must be either 'metrics' or 'params'")

        order_by = [f"{type}.{key} DESC"] if key is not None else None
        run_dict = {}

        for ri in mlflow.list_run_infos(experiment_id, order_by=order_by, max_results = max_results):
            val_str = ""
            run = mlflow.get_run(ri.run_id)

            run_name_str = ''
            if 'mlflow.runName' in run.data.tags:
                run_name_str = 'name ' + run.data.tags['mlflow.runName'] + ', '

            if return_runs:
                run_dict[ri.run_id] = run
            if key is not None:
                rdict = getattr(run.data, type)
                if key in rdict:
                    val = rdict[key]
                    val_str = f", {type}.{key} {val}"

            print(f"- {run_name_str}runid {ri.run_id}, {val_str} ")
        if return_runs:
            return run_dict

def info(experiment: str, run_name: str):
    """
    Print info about run
    """
    with mlflow_ctx(experiment) as experiment_id:
        run = mlflow_run_from_name(run_name, experiment_id)
        ri = run.info
        rd = run.data
        print(f"- run {run_name}, exp {experiment_id}")
        print(f"internal mlflow run id: {ri.run_id}")
        print(f"status: {ri.status}")
        print(f"metrics: \n{rd.metrics}")
        print(f"params: \n{rd.params}")


@click.command(name="init")
@click.option("--project", "-p", "project")
@click.option("--experiment", "-e", "experiment")
def init_cmd(*args, **kwargs):
    return init(*args, **kwargs)

__all__ = [
    'init', 'init_cmd', 'stage', 'add_dependency', 'load_pickle',
    'save_pickle', 'log_model', 'publish_model', 'load_model',
    'restore', 'log_param', 'log_metric', 'log_params', 'log_metrics',
    'list', 'info'
]
