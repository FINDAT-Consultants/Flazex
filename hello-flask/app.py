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
        <title>Welcome</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                background: linear-gradient(135deg, #0f0f0f, #1a1a1a);
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                display: flex;
                align-items: center;
                justify-content: center;
                height: 100vh;
                color: #fff;
                overflow: hidden;
            }

            .message {
                background: rgba(30, 30, 30, 0.9);
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 0 20px rgba(255, 20, 147, 0.5);
                text-align: center;
                animation: fadeIn 1.5s ease-out;
            }

            .message h1 {
                font-size: 3rem;
                background: linear-gradient(90deg, #ff1493, #ff416c, #ff4b2b);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 10px;
            }

            .message p {
                font-size: 1.2rem;
                color: #ccc;
            }

            @keyframes fadeIn {
                from {
                    opacity: 0;
                    transform: translateY(30px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }

            @media (max-width: 600px) {
                .message {
                    padding: 20px;
                }

                .message h1 {
                    font-size: 2.2rem;
                }

                .message p {
                    font-size: 1rem;
                }
            }
        </style>
    </head>
    <body>
        <div class="message">
            <h1>Hello, Beautiful!</h1>
            <p>Welcome to my sensual Flask experience.<br>Everything starts with a single route.</p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    app.run(debug=True)
