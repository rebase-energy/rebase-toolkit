import click
import os
import yaml

from ...util import api_request
from ..utils import run_command

def hpsearch():
    print("Starting hyperparam search")
    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)
    run_id = generate_run_id()
    run_name = f"hps-{run_id[:5]}"
    run_command(f"git commit -a -m 'HP:{run_name}'")
    retc, output = run_command("git config --get remote.origin.url", return_output=True)
    if retc != 0:
        raise RuntimeError(f"Could not extract git repo url: {output}")
    git_remote_url = output.strip()

    with open('hyperparams.yaml', 'r') as f:
        hyperparams = yaml.safe_load(f)
        data = {
            'hyperparams': hyperparams,
            'git_remote_url': git_remote_url,
            'repo_name': repo_name,
            'run_name': run_name,
            'api_key': rb.api_key
        }

    r = api_request.post('platform/v1/model/hpsearch', data=json.dumps(data))
    print("Status", r.status_code)
    data = r.json()
    print('hpsearch id: {}'.format(data['hp_id']))

@click.command(name="hpsearch")
def hpsearch_cmd(*args, **kwargs):
    return hpsearch(*args, **kwargs)

__all__ = ['hpsearch', 'hpsearch_cmd']
