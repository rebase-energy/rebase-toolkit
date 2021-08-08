import rebase as rb


model = rb.Model.load()


prediction = model.predict()
score = 1


rb.Model.log(score)
