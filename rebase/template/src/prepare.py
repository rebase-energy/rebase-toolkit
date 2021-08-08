import rebase as rb
import sys



output_path = argv[1]

params = rb.Params.get('prepare.weather')

df = rb.Weather.get(params)

rb.Dataset.save(output_path, df)
