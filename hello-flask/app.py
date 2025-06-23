from flask import Flask

app = Flask(__name__)

@app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Hello, World!</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #f0f0f0;
                text-align: center;
                padding-top: 100px;
            }
            .message {
                font-size: 2em;
                color: #333;
                background-color: #fff;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
                display: inline-block;
            }
        </style>
    </head>
    <body>
        <div class="message">
            <h1>Hello, World!</h1>
            <p>Welcome to my first Flask application with embedded HTML and CSS.</p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    app.run(debug=True)
