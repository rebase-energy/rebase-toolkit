import modal

# Define a minimal Modal App and Image
app = modal.App()
image = modal.Image.debian_slim().pip_install("cloudpickle")

# Define the Modal function that will run remotely
@app.function(image=image)
def modal_function(x, y):
    return x * y

# Define an async function to use map.aio() correctly
async def run_async_map(x, y):
      # Input values
    
    # Using `app.run()` in an async context
    with app.run():  # Blocking context that runs Modal app
        # Asynchronous map over the inputs
        results = []
        async for result in modal_function.map.aio(x, y):  # Loop through AsyncOrSyncIterable
            results.append(result)  # Collect results asynchronously
    
    return results

# Run the async function
if __name__ == "__main__":
    import asyncio
    inputs = [1, 2, 3, 4, 5]
    result = asyncio.run(run_async_map(inputs, inputs))
    print(result)
