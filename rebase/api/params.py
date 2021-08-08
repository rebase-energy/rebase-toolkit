import yaml


class Params:

    params = None

    @classmethod
    def get(cls, key):
        if not cls.params:
            cls.params = yaml.safe_load(open('params.yaml'))

        return cls.params[key]
