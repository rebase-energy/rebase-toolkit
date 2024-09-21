# rebase.py
import modal
import asyncio
import cloudpickle

import nest_asyncio
nest_asyncio.apply()

app = modal.App()
image = modal.Image.debian_slim().pip_install("cloudpickle==3.0.0", "requests", "pandas", "dill", "joblib", "PyYAML", "mlflow", "dvc", "nest_asyncio")

# Define the Modal function
@app.function(image=image)
def modal_function(func_serialized, params):
    import cloudpickle
    user_function = cloudpickle.loads(func_serialized)
    return user_function(params)

async def run_async_map(func_list, param_list):
    
    # Using `app.run()` in an async context
    with app.run():#show_progress=False):  # Blocking context that runs Modal app
        # Asynchronous map over the inputs
        results = []
        async for result in modal_function.map.aio(func_list, param_list):  # Loop through AsyncOrSyncIterable
            results.append(result)  # Collect results asynchronously
    
    return results

def sweep_sync(user_function, params):
    # Prepare the list of parameter dictionaries
    param_list = [dict(zip(params.keys(), values)) for values in zip(*params.values())]

    # Serialize the user-defined function
    func_serialized = cloudpickle.dumps(user_function)
    
    with app.run(show_progress=False):
        # Map the function over the parameter list
        results = list(modal_function.map([func_serialized] * len(param_list), param_list))

    return results

def sweep(user_function, params):
    # Prepare the list of parameter dictionaries
    param_list = [dict(zip(params.keys(), values)) for values in zip(*params.values())]

    # Serialize the user-defined function
    func_serialized = cloudpickle.dumps(user_function)
    func_list = [func_serialized] * len(param_list)

    #results = asyncio.run(run_async_map(func_list, param_list))

    try:
        # If an event loop is running, use asyncio.ensure_future and get the current loop
        loop = asyncio.get_running_loop()
        results = loop.run_until_complete(run_async_map(func_list, param_list))
    except RuntimeError:  # If no running event loop, use asyncio.run()
        results = asyncio.run(run_async_map(func_list, param_list))

    return results