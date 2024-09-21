# rebase.py
import modal
import cloudpickle

app = modal.App()
image = modal.Image.debian_slim().pip_install("cloudpickle==3.0.0")

def user_defined_function(params):
    a = params["a"]
    b = params["b"]
    return a ** 2 + b ** 2

# Define the Modal function
@app.function(image=image)
def modal_function(params, func_serialized):
    import cloudpickle
    user_defined_function = cloudpickle.loads(func_serialized)
    return user_defined_function(params)

def main():
    # Prepare the list of parameter dictionaries
    params = {
        "a": [1, 2, 3],
        "b": [4, 5, 6]
    }
    param_list = [{"a": a, "b": b} for a, b in zip(params["a"], params["b"])]

    # Serialize the user-defined function
    func_serialized = cloudpickle.dumps(user_defined_function)
    
    with app.run():
        # Map the function over the parameter list
        results = list(
            modal_function.map(
                param_list,
                [func_serialized] * len(param_list)
            )
        )
    
    print(results)

if __name__ == "__main__":
    main()