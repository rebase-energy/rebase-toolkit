import modal

image = modal.Image.debian_slim()
app = modal.App("rebase", image=image)

#@app.function()

def user_defined_function(params):
    a = params["a"]
    b = params["b"]
    return a ** 2 + b ** 2

def main():
    
    params = {
        "a": [1, 2, 3],
        "b": [4, 5, 6]
    }
    param_list = [{"a": a, "b": b} for a, b in zip(params["a"], params["b"])]

    # Create a Modal function from the user-defined function
    modal_function = app.function()(user_defined_function)

    with app.run():
        results = list(modal_function.map(param_list))
    
    print(results)

if __name__ == "__main__":
    main()