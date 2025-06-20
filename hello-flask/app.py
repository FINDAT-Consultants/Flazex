from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello():
    return '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Hello from EC2</title>
        <style>
            body {
                margin: 0;
                height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                overflow: hidden;
            }

            .zoom-text {
                font-size: 3em;
                color: white;
                padding: 20px 40px;
                border-radius: 15px;
                background: rgba(255, 255, 255, 0.1);
                box-shadow: 0 8px 32px 0 rgba( 31, 38, 135, 0.37 );
                backdrop-filter: blur(6.5px);
                -webkit-backdrop-filter: blur(6.5px);
                border: 1px solid rgba(255, 255, 255, 0.18);
                animation: zoomInOut 3s infinite ease-in-out;
                transition: transform 0.5s ease-in-out;
            }

            @keyframes zoomInOut {
                0% { transform: scale(1); }
                50% { transform: scale(1.15); }
                100% { transform: scale(1); }
            }

            .zoom-text:hover {
                transform: scale(1.25);
                cursor: pointer;
            }
        </style>
    </head>
    <body>
        <div class="zoom-text" onclick="restartAnimation()">Hello, World from EC2!</div>

        <script>
            function restartAnimation() {
                const el = document.querySelector('.zoom-text');
                el.style.animation = 'none';
                el.offsetHeight; // trigger reflow
                el.style.animation = null;
            }
        </script>
    </body>
    </html>
    '''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
