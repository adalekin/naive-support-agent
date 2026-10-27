# Pixmill

Pixmill is an online service that makes pictures and videos with AI models. A client buys a plan
and gets credits; every generation costs credits. The interface has a feed of the client's
generations, an image editor and a video editor.

## Plans and credits

- A monthly plan gives credits that last 60 days from the day they are added: twice as long as the
  month. A pack of credits bought on top of a plan lasts 90 days.
- Credits of a plan can be spent only while a plan is active. When the plan lapses, the credits
  are frozen until the next payment, and their 60 days keep running.
- Credits with the earliest end date are spent first.
- Credits that expired by their term are not restored.
- Support adds bonus credits in the admin panel, section Credits.
- The terms are on the pricing page and in the offer the client accepts at payment.

## Models

- Image models: Vista 2, Vista 2.1, Vista Pro. Video models: Reel 3, Reel 3 Edit.
- A model may refuse a request without saying why. Pixmill then retries the same request up to
  three times. A failed generation returns its credits to the client automatically.
- Every picture and video sent to a model first passes Pixmill's own check for real people, so
  that the model does not refuse it.
- The video editor (cut, background, framing, the "lasso" edit) runs on Reel 3 Edit.
