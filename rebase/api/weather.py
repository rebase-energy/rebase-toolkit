import rebase.util.api_request as api_request
import pandas as pd
import json
import pickle
import rebase as rb
import hashlib

def get_cached_weather(cache_file):
    try:
        with open(cache_file, 'rb') as f:
            return pickle.load(f)
    except:
        return None

def save_cached_weather(cache_file, df):
    try:
        with open(cache_file, 'wb') as f:
            pickle.dump(df, f)
    except:
        pass


def json_to_df(json_data):
    df = pd.read_json(json_data)
    df.index = pd.MultiIndex.from_arrays(
             [pd.to_datetime(df['ref_datetime'].values),
             pd.to_datetime(df['valid_datetime'].values)],
             names=['ref_datetime', 'valid_datetime'])
    # Drop now duplicated index columns
    df = df.drop(columns=['ref_datetime', 'valid_datetime'])
    return df


def resample(df, resolution):
    return df.reset_index() \
                .set_index(df.index.get_level_values(1)) \
                .groupby(df.index.get_level_values(0)) \
                .resample(resolution).mean().interpolate(method='linear')

class Weather():

    @classmethod
    def get(cls, params):
        path = '/weather/v2/query'
        response = api_request.get(path, params=params)
        return response.json()


    @classmethod
    def historical(cls, params, resolution=None):
        path = '/weather/v1/get_nwp'
        json_params = json.dumps(params)
        params_hash = hashlib.md5(json_params.encode('utf-8')).hexdigest()
        os.makedirs(rb.cache_dir, exist_ok=True)
        cache_file = '{}/{}.pickle'.format(rb.cache_dir, params_hash)

        df = get_cached_weather(cache_file)
        if df is None:
            response = api_request.get(path, params={'query_params': json_params})
            if response.status_code != 200:
                raise Exception('Failed retrieving weather data, status: {}, data: {}'.format(response.status_code, response.content.decode('utf-8')))
            try:
                df = json_to_df(response.text)
            except Exception as e:
                print("Error converting to json: {}".format(response.text))
                raise e

        save_cached_weather(cache_file, df)

        if resolution:
            df = resample(df, resolution)

        return df

    @classmethod
    def operational(cls, params, resolution=None):
        path = '/weather/v1/get_latest_nwp'
        json_params = json.dumps(params)

        response = api_request.get(path, params={'query_params': json_params})
        if response.status_code != 200:
            raise Exception('Failed retrieving weather data, status: {}'.format(response.status_code))
        df = json_to_df(response.text)

        if resolution:
            df = resample(df, resolution)

        return df
