from google import genai
import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    print("Failed to load .env file installing needed pkg...")
    def install():
        subprocess.check_call([sys.executable, "-m","pip","install","python-dotenv"])
    try:
        install()
    except:
        quit("failed to install python-dotenv!")
    else:
        print("loading .env..")
        from dotenv import loaddot_env
        load_dotenv()

else:
    print("loaded .env...")


def send_to_Ai(Text : str):
    Clinet = genai.Client(api_key=os.getenv("GEMINI_APT_KEY"))

    result = Clinet.interactions.create(
       model = "gemini-3.8-flash",
       input=Text
    )

    print(result.output_text)

send_to_Ai(input("say:"))

