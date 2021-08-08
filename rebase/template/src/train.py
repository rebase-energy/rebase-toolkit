import rebase as rb
import sys

input_path = sys.argv[1]
output_path = sys.argv[2]

rb.Dataset.load(input_path)

model = None

rb.Model.save(output_path, model)
