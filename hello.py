import os
from ollama import Client

client = Client(
    host="http://127.0.0.1:11434",
    headers={'Authorization': 'Bearer ' + os.environ.get('OLLAMA_API_KEY')}
)

messages = [
  {
    'role': 'user',
    'content': 'say hello',
  },
]

for part in client.chat('gpt-oss:120b-cloud', messages=messages, stream=True):
  print(part['message']['content'], end='', flush=True)
