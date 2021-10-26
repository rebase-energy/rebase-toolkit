import copy
import pickle
from .utils import run_commands,run_command
import os

g_current_context = None

def current_context(from_file=False):
    """
    Get the current context.
    from_file = True, then restore ctx from file
    .context.pkl instead of memory
    """
    return Context.current(from_file=from_file)

class Stage(object):
    def __init__(self, name, params=None, *args, **kwargs):
        self.name = name
        self.params = params
        self.dependencies = {}
        self.outputs = {}

    def save_dependencies(self):
        self.saved_deps = copy.deepcopy(self.dependencies)

    def clear_dependencies(self):
        self.dependencies = {}

    def clear_outputs(self):
        self.outputs = {}

    def _add_dep(self, dep_dict, path, name=None, meta=None, **kwargs):
        dep_dict[name or path] = { 'name': name or path,
                                    'path': path,
                                    'meta': meta or {},
                                    **kwargs
                                  }

    def add_dependency(self, path, name=None, meta=None, externals=None, **kwargs):
        self._add_dep(self.dependencies, path, name=name, meta=meta, **kwargs)

    def add_output(self, path, name=None, meta=None, **kwargs):
        self._add_dep(self.outputs, path, name=name, meta=meta, **kwargs)

    def get_outputs(self):
        return self.outputs

    def get_dependency(self, name):
        return self.dependencies.get(name, None)

    def get_dependency_by_path(self, path):
        deps = [d for d in self.dependencies.values() if d['path'] == path]
        if len(deps) == 0:
            return None
        elif len(deps) > 1:
            logging.error(f"Dependency with path {path} is registered multiple times")
            return None
        else:
            return deps[0]

class Context(dict):
    def __init__(self, *args, **kwargs):
        self.stages = {}
        self.set_stage("root")
        self.curr_run = None
        self.artifact_uri = None
        self['envs'] = {}

    def set_current_run(self, run_name):
        self.curr_run = run_name

    def clear_run(self):
        self.curr_run = None

    def get_run(self):
        return self.curr_run

    def set_stage(self, name, params=None):
        if name not in self.stages:
            self.stages[name] = Stage(name, params=params)
        self._current_stage = self.stages[name]

    def current_stage(self):
        return self._current_stage

    def save(self):
        _, git_path = run_command("git rev-parse --show-toplevel",
                                  return_output=True)
        with open(f'{git_path.strip()}/.context.pkl', 'wb+') as f:
            pickle.dump(self, f)

    def load_env(self):
        """
        Set process environment variables from ctx
        """
        for k,v in g_current_context['envs'].items():
            os.environ[k] = v

    @staticmethod
    def load():
        global g_current_context
        _, git_path = run_command("git rev-parse --show-toplevel",
                                  return_output=True)
        git_path = git_path.strip()
        ctx_path = f'{git_path}/.context.pkl'
        try:
            with open(ctx_path, 'rb') as f:
                return pickle.load(f)
        except:
            raise OSError(f"{ctx_path} could not be loaded. Make sure you ran rb init")

    def generate_dvc_pipeline(self, prev_dvc = None):
        if prev_dvc is None or 'stages' not in prev_dvc:
            dvc = {'stages': {}}
        else:
            dvc = prev_dvc

        for stage_name in sorted(list(self.stages.keys())):
            if stage_name == "root":
                continue

            stage = self.stages[stage_name]

            if stage_name in dvc['stages']:
                dvc_stage = dvc['stages'][stage_name]

                # deps
                if 'deps' in dvc_stage:
                    dvc_stage['deps'] = set(dvc_stage['deps'])
                    for k,d in stage.dependencies.items():
                        if d['path'] not in dvc_stage['deps']:
                            dvc_stage['deps'].add(d['path'])
                    dvc_stage['deps'] = sorted(list(dvc_stage['deps']))
                else:
                    dvc_stage['deps'] = sorted([d['path'] for k, d in stage.dependencies.items()])

                # outs
                if 'outs' in dvc_stage:
                    dvc_stage['outs'] = set(dvc_stage['outs'])
                    for k,d in stage.outputs.items():
                        if d['path'] not in dvc_stage['outs']:
                            dvc_stage['outs'].add(d['path'])
                    dvc_stage['outs'] = sorted(list(dvc_stage['outs']))
                else:
                    dvc_stage['outs'] = sorted([d['path'] for k, d in stage.outputs.items()])
            else:
                dvc_stage = {
                    "cmd": f"echo \"specify 'python yourpath/{stage_name}.py' here\"",
                    "deps": sorted([d['path'] for k, d in stage.dependencies.items()]),
                    "outs": sorted([d['path'] for k, d in stage.outputs.items()])
                }
            if stage.params:
                dvc_stage["params"] = sorted([f"{stage_name}.{k}" for k in stage.params.keys()])
                #dvc_stage["params"] = sorted(list(stage.params.keys()))

            dvc['stages'][stage_name] = dvc_stage
        return dvc

    @staticmethod
    def current(from_file=False):
        global g_current_context

        if from_file:
            g_current_context = Context.load()
            g_current_context.load_env()

        if g_current_context is None:
            g_current_context = Context()

        return g_current_context

__all__ = ['Context', 'current']
