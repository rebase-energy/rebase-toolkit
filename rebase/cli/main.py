import argparse
import yaml
import pkgutil
import os
import ast
import mlflow
import json
import rebase.util.api_request as api_request
import rebase as rb

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


def init():
    print("Initializing")

    setup_template()
    print("Created template files in src/")



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




def run(param_list, tag):
    update_params_file(param_list)
    #with mlflow.start_run() as run:
    #if tag:
    #    mlflow.set_tag('hyperparam_search', tag)
    #os.system('dvc repro')
    os.system('mlflow run --no-conda .')


def hyperparam_search():
    print("Starting hyperparam search")
    with open('hyperparams.yaml', 'r') as f:
        hyperparams = yaml.safe_load(f)
        data = {
            'hyperparams': hyperparams['train']
        }

    r = api_request.post('platform/v1/model/hpsearch', data=json.dumps(data))
    print("Status", r.status_code)
    data = r.json()
    print('hpsearch id: {}'.format(data['hp_id']))

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('command')
    parser.add_argument('-p', action='append')
    parser.add_argument('-t')

    args = parser.parse_args()


    if args.command == 'init':
        init()
    elif args.command == 'hpsearch':
        hyperparam_search()
    elif args.command == 'run':
        run(args.p, args.t)
