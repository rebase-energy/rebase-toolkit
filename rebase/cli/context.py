import copy

g_current_context = None

def  current_context():
	return Context.current()


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
		dep_dict[name] = { 'name': name or path,
						   'path': path,
						   'meta': meta or {},
						   **kwargs
						 }

	def add_dependency(self, path, name=None, meta=None, externals=None, **kwargs):
		self._add_dep(self.dependencies, path, name=name, meta=meta, **kwargs)
		
	def add_output(self, path, name=None, meta=None, **kwargs):
		self._add_dep(self.outputs, path, name=name, meta=meta, **kwargs)

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

	def set_stage(self, name, params=None):
		if name not in self.stages:
			self.stages[name] = Stage(name, params=params)
		self._current_stage = self.stages[name]

	def current_stage(self):
		return self._current_stage

	def generate_dvc_pipeline(self):
		dvc = {'stages': {}}
		for stage_name in sorted(list(self.stages.keys())):
			if stage_name == "root":
				continue

			stage = self.stages[stage_name]
			dvc_stage = {
				"cmd": f"echo \"{stage_name}.py not implemented\"",
				"deps": sorted([d['path'] for k, d in stage.dependencies.items()]),
				"outs": sorted([d['path'] for k, d in stage.outputs.items()])}
			if stage.params:
				dvc_stage["params"] = sorted(list(stage.params.keys()))
			dvc['stages'][stage_name] = dvc_stage
		return dvc
	
	@staticmethod
	def current():
		global g_current_context

		if g_current_context is None:
			g_current_context = Context()

		return g_current_context

__all__ = ['Context', 'current']
