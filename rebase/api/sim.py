import rebase.util.api_request as api_request
import requests
import json
import copy
import pandas as pd
import subprocess
import pip._internal as pip
from sklearn.model_selection import ParameterGrid
import itertools

base = 'platform/v1'

test = {
    'type': 'load',
    'nwps': ['DWD_ICON-EU', 'NCEP_GFS'],
    'variables': [
        {'name': 'Temperature', 'lag': [-4, -3, -2, -1, 1, 2, 3, 4]},
        {'name': 'SolarDownwardRadiation'},
    ],
    'calendar': ['holidays', 'hourOfDay']
}


class ApiConnector():

    def __init__(self, **kwargs):
        if kwargs['id']:
            if len(kwargs.keys()) > 1:
                kwargs.pop('id')
                keys = ','.join(kwargs.keys())
                raise Exception(f'If id is specified, {keys} must be omitted')

class Site:

    id = None
    def __init__(self,
        id=None,
        name=None,
        latitude=None,
        longitude=None
    ):
        self.id = id

        if id:
            if name or latitude or longitude:
                raise Exception('If id is specified, name, latitude and longitude must be omitted')
            d = self._get()
            name = d['name']
            latitude = d['latitude']
            longitude = d['longitude']

        self.name = name
        self.latitude = latitude
        self.longitude = longitude

    # @classmethod
    # def from_dict(cls, d):
    #     self.id = d['id']
    #     self.name = d['name']
    #     self.latitude = d['latitude']
    #     self.longitude = d['longitude']

    def __str__(self):
        return f'{self.to_dict()}'

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'latitude': self.latitude,
            'longitude': self.longitude
        }

    def _get(self):
        response = api_request.get(f'{base}/site/{self.id}')
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise Exception(f'Site {self.id} does not exist')


    def _post(self):
        path = f'{base}/site/create'
        json_site = json.dumps({**self.to_dict(), **test})
        return api_request.post(path, data=json_site)

    def create(self):
        response = self._post()
        if response.status_code == 200:
            data = response.json()
            self.id = data['site_id']
            print(f'Site <{self.name}, {self.id}> created!')
        else:
            raise Exception('Bad status code:', response.status_code)


    # FIXME: NEEDS new route to update site
    #def update(self):
    #    response = self._post()
    #    if response.status_code == 200:
    #        print(f'Site {self.id} updated!')


    def delete(self):
        path = f'{base}/site/{self.id}'
        response = api_request.delete(path)
        if response.status_code == 200:
            self.id = None
            print(f'Success. Site: <{self.name }, {self.id}> was deleted.')



# base component
class Component:
    id = None

    name = None

    def to_dict(self):
        raise NotImplementedError('Component must implement to_dict()')

    def copy(self):
        return copy.deepcopy(self)

class Data():

    def __init__(self, data):
        self.data = data

class ElectricityDemand(Component):

    name = 'electricity_demand'

    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return {
            'type': 'electricity_demand',
            'fields': {
                #'data': self.data
            }
        }

    def get_data(self, **kwargs):
        return self.data


class PV(Component):

    def __init__(self, azimuth=None, tilt=None, capacity=None, data=None):
        self.azimuth = azimuth
        self.tilt = tilt
        self.capacity = capacity
        self.data = data

    def to_dict(self):
        return {
            'type': 'pv',
            'fields': {
                'azimuth': self.azimuth,
                'tilt': self.tilt,
                'capacity': self.capacity
            }
        }

    def get_data(self, **kwargs):
        if self.data:
            return self.data
        return self.forecast(**kwargs)

    def forecast(self, latitude=None, longitude=None, start_date=None, end_date=None, freq='1H', format='DataFrame'):
        url = 'https://energydatamap.com/api/solar'
        params = {
            'latitude': latitude,
            'longitude': longitude,
            'capacity_kw': self.capacity,
            'azimuth': self.azimuth,
            'tilt': self.tilt,
            'start_date': start_date,
            'end_date': end_date,
            'frequency': freq
        }
        r = requests.get(url, params=params)
        data = r.json()
        if format == 'json':
            return data
        return pd.DataFrame(data={'value': data['Clearsky_Forecast']}, index=pd.to_datetime(data['valid_datetime']))

class Battery(Component):

    def __init__(self,
        capacity=None,
        min_level=0.1,
        charge_max=None,
        discharge_max=None,
        efficiency_charge=0.9,
        efficiency_discharge=0.9
    ):
        self.min_level = min_level
        self.capacity = capacity
        self.charge_max = capacity if not charge_max else charge_max
        self.discharge_max = capacity if not discharge_max else discharge_max
        self.efficiency_charge = efficiency_charge
        self.efficiency_discharge = efficiency_discharge

    def to_dict(self):
        return {
            'type': 'battery',
            'fields': {
                'min_level': self.min_level,
                'capacity': self.capacity,
                'charge_max': self.charge_max,
                'discharge_max': self.discharge_max,
                'efficiency_charge': self.efficiency_charge,
                'efficiency_discharge': self.efficiency_discharge
            }
        }



class Grid(Component):

    def __init__(self,
        fee_energy=None,
        fee_power=None,
        overcharge_penalty=None,
        power_contract=None
    ):
        self.fee_energy = fee_energy
        self.fee_power = fee_power
        self.overcharge_penality = overcharge_penalty
        self.power_contract = power_contract


    def to_dict(self):
        return {
            'type': 'grid',
            'fields': {
                'fee_energy': self.fee_energy,
                'fee_power': self.fee_power,
                'overcharge_penality': self.overcharge_penality,
                'power_contract': self.power_contract
            }
        }

class SystemCreator:

    components = []
    def __init__(self, site=None, base=None):
        self.base = base
        self.site = site if site else base.site
        if base:
            self.components = [{'comp': comp, 'params': []} for comp in base.components]


    def add(self, comp, params):
        self.components.append({'comp': comp, 'params': params})

    def get_search_space(self):
        all_comp_list = []
        for c in self.components:
            comp_list = []
            if len(c['params']) < 1:
                comp_list.append(c['comp'].copy())
            else:
                param_grid = ParameterGrid(c['params'])
                for params in param_grid:
                    new_comp = c['comp'].copy()
                    for p in params:
                        setattr(new_comp, p, params[p])
                    comp_list.append(new_comp)
            all_comp_list.append(comp_list)

        # list of all possible combos of components
        return list(itertools.product(*all_comp_list))

    def create(self):
        search_space = self.get_search_space()
        systems = []
        for comp_tuple in search_space:
            s = System(self.site, components=list(comp_tuple))
            s.create()
            systems.append(s)
        return systems

class System:

    id = None
    def __init__(self, site, components=[]):
        self.site = site
        self.components = components


    def add(self, comp):
        self.components.append(comp)

    def to_dict(self):
        return {
            'site_id': self.site.id,
            'components': [comp.to_dict() for comp in self.components]
        }

    def copy(self):
        return copy.deepcopy(self)


    def create(self):
        path = f'{base}/system'
        response = api_request.post(path, data=json.dumps(self.to_dict()))
        if response.status_code == 200:
            data = response.json()
            self.id = data['id']
            print(f'System <{self.id}> created!')
        else:
            raise Exception('Bad status code:', response.status_code)



class Model:

    is_installed = False
    def __init__(self, repo):
        self.repo = repo

    def to_dict(self):
        pass

    def run(self, data):
        #if not self.is_installed:
        #print("installing")
        #subprocess.run(["git", "clone", self.repo])
        #print("done")
        #subprocess.run(["cd", "d3a"])
        #print(subprocess.run(["ls"]))
        #import d3a.run
        #subprocess.run(["cd", ".."])
        #d3a.run()
        #print(d3a.d3a)
        # rebase run?
        #pip.main(['install', self.repo])
        #from d3a.microgrid import microgrid_model
        #print(d3a.microgrid)
        pass



    def get_inputs(self):
        example = {
            'PV': {
                'data': 'generation'
            },
            'Battery': {
                'min_level': 'battery_min_level',
                'capacity': 'battery_capacity'
            },
            'Grid': {
                'fee_energy': 'grid_fee_energy',
                'fee_power': 'grid_fee_power'
            },
            'ElectricityDemand': {
                'data': 'demand'
            }
        }
        return example

class ModelChain:

    def __init__(self, system, model, tag=None):
        self.system = system
        self.model = model
        self.tag = tag

    @classmethod
    def from_dict(cls, d):
        self.system = d['system']
        self.model = Model.from_dict(d['model'])
        self.tag = d['tag']

    def to_dict(self):
        return {
            'system': self.system.to_dict(),
            'model': self.model.to_dict(),
            'tag': self.tag
        }

    def prepare_input(self, start_date, end_date):
        expected_inputs = self.model.get_inputs()

        all_input = {}
        merged_df = None

        # iterate throught system compnents
        for comp in self.system.components:
            print(comp)
            class_name = type(comp).__name__

            # if component class name is in expected model inputs
            if class_name in expected_inputs:
                comp_inputs = expected_inputs[class_name]

                # iterate through keys in expected inputs for component
                for k in comp_inputs:
                    mapped_key = comp_inputs[k]
                    if k == 'data':
                        # load data from component
                        df = comp.get_data(
                                    latitude=self.system.site.latitude,
                                    longitude=self.system.site.longitude,
                                    start_date=start_date,
                                    end_date=end_date
                                )
                        formatted_df = pd.DataFrame(data={mapped_key: df['value'], 'valid_datetime': pd.to_datetime(df.index.values.tolist())})
                        # merge components' data into one dataframe with equal length
                        if merged_df is None:
                            merged_df = formatted_df
                        else:
                            merged_df = merged_df.merge(formatted_df, on='valid_datetime')
                    else:
                        all_input[mapped_key] = getattr(comp, k)
        merged_df = merged_df.drop(columns=['valid_datetime'])
        data_dict = merged_df.to_dict(orient='list')
        return {**all_input, **data_dict}

    def run(self, start_date, end_date):
        all_input = self.prepare_input(start_date, end_date)
        return self.model.run(all_input)






class Run():

    def __init__(self, tasks):
        self.tasks = tasks


    def stop(self):
        pass

    def result(self):
        return [
            task.result() for task in self.tasks
        ]

    def status(self):
        statuses = []
        current = 'finished'
        for task in self.tasks:
            st = task.status()
            if st['current'] != 'finished':
                current = st['current']

            statuses.append(st)
        return {
            'current': current,
            'runs': statuses
        }

class Task:

    def __init__(self, mc):
        self.model_chain = mc

    def run(self, start_date, end_date):
        mc = self.model_chain
        print(f'Starting run for: {mc.system.id}')
        url = f'{base}/system/{mc.system.id}/model/chain'

        upload_data = {}
        for comp in mc.system.components:
            if comp.name == 'electricity_demand':
                if comp.data is not None:
                    df = comp.data.reset_index()
                    upload_data = {'demand': df.to_dict(orient='list')}

        resp = api_request.post(url, data=json.dumps({}))
        if resp.status_code == 200:
            data = resp.json()
            mc.id = data['id']
            url = f'{base}/model/chain/run/{mc.id}'
            run_data = {'run': {'start_date_utc': start_date, 'end_date_utc': end_date}, **upload_data}
            resp = api_request.post(url, data=json.dumps(run_data))
            data = resp.json()
            self.model_chain_run_id = data['id']
        else:
            raise Exception(f'Task run(), bad status code: {resp.status_code}')


    def result(self):
        url = f'{base}/model/chain/run/{self.model_chain_run_id}'
        resp = api_request.get(url)
        if resp.status_code == 200:
            data = resp.json()
            result = data['result']
            return {
                'timeseries': pd.DataFrame(data=result['timeseries']),
                'total': result['total']
            }
        return None


    def status(self):
        url = f'{base}/model/chain/run/{self.model_chain_run_id}'
        resp = api_request.get(url)
        if resp.status_code == 200:
            data = resp.json()
            return {'id': self.model_chain_run_id, **data['status']}
        raise Exception(f'Task status(), bad status code: {resp.status_code}')



class TaskGroup:

    def __init__(self, tasks):
        self.tasks = [Task(t) for t in tasks]

    def run(self, start_date=None, end_date=None):
        for task in self.tasks:
            task.run(start_date, end_date)

        return Run(self.tasks)
