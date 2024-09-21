# main.py
import rebase as rb

def user_defined_function(params):
    a = params["a"]
    b = params["b"]
    return a ** 2 + b ** 2

params = {
    "a": [1, 2, 3],
    "b": [4, 5, 6]
}

results = rb.sweep(user_defined_function, params)
print(results)
