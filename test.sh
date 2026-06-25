clear
invoke_url='http://192.168.1.234:32359/v1/chat/completions'
accept_header='Accept: application/json'
content_type_header='Content-Type: application/json'

data=$'{
  "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4",
  "messages": [
    {
      "role": "user",
      "content": "Hello, can you help me with a coding problem?"
    }
  ],
  "temperature": 1,
  "top_p": 1,
  "max_tokens": 16384,
  "seed": 42,
  "stream": false
}'

response=$(curl --silent -w "\n%{http_code}" --request POST \
  --url "$invoke_url" \
  --header "$accept_header" \
  --header "$content_type_header" \
  --data "$data"
)

echo "$response" | jq
