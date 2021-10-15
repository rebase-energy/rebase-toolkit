import rebase.util.api_request as api_request
import json
import pandas as pd
import requests
import logging

class SiteTemplate():


    def __init__(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude

class Site():

    base_path = 'platform/v1'

    @classmethod
    def create(cls, site_config):
        """Create a new site

        Args:
            site_config (dict): config for site to create

        Returns:
            str: the site id for the newly created site

        Raises:
            rebase.InvalidUsageError: if site_config is missing some required fields

        Example::

            >>> site_config = {
                'latitude': 51,
                'longitude': 7
            }
            >>> site_id = rb.Site.create(site_config)
            >>> print(site_id)
            4ab82692-3944-4069-9cbb-f9c59513c1c3
        """
        path = '{}/site/create'.format(cls.base_path)
        json_site = json.dumps(site_config)
        response = api_request.post(path, data=json_site)
        return response.json()


    @classmethod
    def get(cls, site_id):
        """Get a site by id

        Args:
            site_id (str): the id of the site

        Returns:
            dict: the config of the site

            Example::

                >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
                >>> site_config = rb.Site.get(site_id)
                >>> print(site_config)
                {
                    'site_id': '4ab82692-3944-4069-9cbb-f9c59513c1c3',
                    'type': 'solar'
                }

        Raises:
            rebase.NotFoundError: if specified site does not exist
        """
        r = api_request.get('{}/site/{}'.format(cls.base_path, site_id))
        return r.json()

    @classmethod
    def delete(cls, site_id):
        """Delete a site

        Args:
            site_id (str): id of site to delete

        Raises:
            rebase.NotFoundError: if specified site does not exist
        Example::

            >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
            >>> rb.Site.delete(site_id)
            Success. Site: 4ab82692-3944-4069-9cbb-f9c59513c1c3 was deleted.
        """
        path = '{}/site/{}'.format(cls.base_path, site_id)
        response = api_request.delete(path)
        if response.status_code == 200:
            print('Success. Site: {} was deleted.'.format(site_id))
        else:
            raise Exception('Delete site failed. Site: {} was NOT deleted. API status code: {}'.format(site_id, response.status_code))

    @classmethod
    def observation(cls, site_id, start_date, end_date=None):
        path = '{}/site/observation/{}'.format(cls.base_path, site_id)
        params = {
            'start_date': start_date,
            'end_date': end_date
        }
        response = api_request.get(path, params=params)
        if response.status_code == 200:
            data = response.json()
            df = pd.DataFrame(data={'observation': data['power_kw']}, index=pd.to_datetime(data['valid_time']))
            df.index.name = 'valid_time'
            return df
        else:
            print(response.status_code)

        return None


    @classmethod
    def forecast(cls, site_id, start_date=None, end_date=None, type='prioritized'):
        """Get the latest forecast for a site

        Args:
            site_id (str): id of the site
            type (str): type of forecast to return

                - ``prioritized`` returns best forecast at the time

                - ``ai`` only returns AI forecasts

                - ``physical`` only returns physical forecasts

                Default ``prioritized``

        Returns:
            dict:
            Returns a dict with the following format:
            ::

                {
                    'type' (str): # type same as params,
                    'ref_time' (DateTime): # date when forecast data updated,
                    'df' (pandas.DataFrame): # dataframe with forecast data
                }

            Example::

                >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
                >>> data = rb.Site.forecast(site_id)
                >>> print(data['df'])
                                               forecast
                    valid_time
                    2020-10-14 00:00:00+00:00       77.3
                    2020-10-14 00:15:00+00:00       86.1
                    ...                             ...
                    2020-10-17 23:30:00+00:00       87.0
                    2020-10-17 23:45:00+00:00       86.6
        """
        path = '{}/site/forecast/latest/{}'.format(cls.base_path, site_id)
        params = {
            'type': type
        }
        if start_date is not None and end_date is not None:
            params['start_date'] = start_date
            params['end_date'] = end_date
        
        response = api_request.get(path, params=params)
        if response.status_code == 200:
            data = response.json()
            df = pd.DataFrame(data={'forecast': data['forecast']}, index=pd.to_datetime(data['valid_time']))
            df.index.name = 'valid_time'
            return {
                'type': data['type'],
                'ref_time': data['ref_time'],
                'df': df
            }


    @classmethod
    def production(cls, site_id, start_date=None, end_date=None, resolution="1H"):
        """Get the historical production for a site

        Args:
            site_id (str): id of the site
           
        Returns:
            dict:
            Returns a dataframe with production data

            Example::

                >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
                >>> data = rb.Site.production(site_id)
                >>> print(data)
                                                power_kw
                    valid_time
                    2020-10-14 00:00:00+00:00       77.3
                    2020-10-14 00:15:00+00:00       86.1
                    ...                             ...
                    2020-10-17 23:30:00+00:00       87.0
                    2020-10-17 23:45:00+00:00       86.6
        """
        path = '{}/site/production/{}'.format(cls.base_path, site_id)
       
        params = {}        
        if start_date is not None and end_date is not None:
            params['date_start'] = start_date
            params['date_end'] = end_date
        if resolution is not None:
            params['resolution'] = resolution
        
        response = api_request.get(path, params=params)
        
        if response.status_code == 200:
            data = response.json()
            df = pd.DataFrame(data={'power_kw': data['power_kw']}, index=pd.to_datetime(data['valid_time']))
            df.index.name = 'valid_time'
            return df
        else:
            print(response.content.decode('utf-8'))
            

    @classmethod
    def list(cls):
        """List all of your sites

        Returns:
            List: list with one dict per site

            Example::

                >>> sites = rb.Site.list()
                >>> print(sites)
                [
                    {'site_id': ..., }, # site 1
                    ...,
                    {'site_id': ...}, # site N
                ]
        """
        path = '{}/sites'.format(cls.base_path)
        response = api_request.get(path)
        return response.json()

    @classmethod
    def predicters(cls, site_id):
        path = '{}/site/models/{}'.format(cls.base_path, site_id)
        # FIXME: Implement logic to list models for site

        response = api_request.get(path)
        models = response.json()
        return models


    @classmethod
    def train(cls, site_id):
        """Start training for your site

        Args:
            site_id (str): id of site to train

        Raises:
            rebase.TrainingNotReadyError : if you have not uploaded observation data to train on for this site

        Example::

            >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
            >>> rb.Site.train(site_id)

        """
        path = '{}/site/train/{}'.format(cls.base_path, site_id)
        response = api_request.post(path)
        if response.status_code == 200:
            print("Success! Queued training for site: {}".format(site_id))
        else:
            raise Exception('Failed for site: {}. API status code: {}'.format(site_id, response.status_code))


    @classmethod
    def status(cls, site_id):
        """Get the training status of your site.

        Args:
            site_id (str): id of site to get status of

        Returns:
            dict:

            Returns a dict with the training status.

            Possible states:

            - ``queued`` - training is queued

            - ``training`` - is currently training

            - ``complete`` - training is complete

            - ``retry`` - training failed but is retrying

            - ``failed`` - training failed (no more retry)

            Example::

                >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
                >>> rb.Site.status(site_id)
                {
                    'status': 'complete',
                    'history': [
                        {'state': 'queued', 'timestamp_utc': '2020-10-12 13:04:17'},
                        {'state': 'training', 'timestamp_utc': '2020-10-12 13:04:22'},
                        {'state': 'complete', 'timestamp_utc': '2020-10-12 13:05:23'},
                    ]
                }
        """
        path = '{}/site/train/state/{}'.format(cls.base_path, site_id)
        r = api_request.get(path)

        status = {'status': None, 'history': []}
        if r.status_code == 200:
            data = r.json()
            if len(data) > 0:
                status['status'] = data[-1]['state']
                status['history'] = data
        return status

    @classmethod
    def upload(cls, site_id, df):
        """Upload observed data for your site. This data is used when training a model.

        Args:
            site_id (str): id of site to upload data for
            df (pandas.DataFrame): DataFrame with the following format

                ::

                    >>>
                             valid_time                observation

                    0        2020-01-22 00:00:00+00:00       126.3
                    1        2020-01-22 00:15:00+00:00       122.7
                    .        ...                             ...
                    n-1      2020-10-17 23:30:00+00:00       169.2
                    n        2020-10-17 23:45:00+00:00       176.6

        Example::

            >>> import pandas as pd
            >>> site_id = '4ab82692-3944-4069-9cbb-f9c59513c1c3'
            >>> df = pd.read_csv('example_data.csv')
            >>> rb.Site.upload(site_id, df)
            Success!
        """
        path = '{}/site/measurement/upload_2/{}'.format(cls.base_path, site_id)
        df = df.dropna()
        data = {
            'valid_time': pd.to_datetime(df['valid_time']).dt.strftime('%Y-%m-%dT%H:%M:%SZ').values.tolist(),
            'measurement': df['observation'].values.tolist(),
            'type': 'ProductionPower'
        }
        response = api_request.post(path, data=json.dumps(data))
        if response.status_code == 200:
            print("Success! Data from {} to {} was uploaded.".format(df.iloc[0]['valid_time'], df.iloc[-1]['valid_time']))
        else:
            print(response.status_code)
