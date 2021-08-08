import rebase as rb
import sys

input_path = sys.argv[1]
output_path = sys.argv[2]

data = rb.Dataset.load(input_path)


rb.Dataset.save(data, output_path)
