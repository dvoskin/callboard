# Local-only dev launcher (not deployed). Pins a port and runs the Flask app.
import os
os.environ.setdefault("PORT", "8812")
import app
app.app.run(debug=False, port=int(os.environ["PORT"]), use_reloader=False, threaded=True)
