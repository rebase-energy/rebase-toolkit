import rebase.util.api_request as api_request
import requests
import json
import dill

def project_init(project_name):
    r = api_request.post('platform/v1/sdk/project/init',
                        data=json.dumps({'project': project_name}))
    if r.status_code != 200:
        raise Exception(f"Error initializing project {project_name}: {r.content.decode('utf-8')}")
    return r.json()
