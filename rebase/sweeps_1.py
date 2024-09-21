import modal
# I do not really understand why this is not working. Would be good to understnd this better. 



#image = modal.Image.debian_slim()
image = modal.Image.debian_slim().pip_install("cloudpickle==3.0.0", "requests", "pandas", "dill", "joblib", "PyYAML", "mlflow", "dvc")
app = modal.App("rebase")#, image=image)

# Does not work define the function here
#modal_function = app.function(image=image)

def sweep_1(user_function, params):
    param_list = [dict(zip(params.keys(), values)) for values in zip(*params.values())]

    #globals()['user_function'] = user_function
    #modal_f = modal_function(user_function)
    with app.run():
        modal_function = app.function(image=image)(user_function)
        results = list(modal_function.map(param_list))
    