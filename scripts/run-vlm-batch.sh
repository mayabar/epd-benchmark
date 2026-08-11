#!/usr/bin/env bash

URLS=(
  "http://images.cocodataset.org/val2017/000000039769.jpg"
  "https://plus.unsplash.com/premium_photo-1667030474693-6d0632f97029?q=80&w=1287&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"
  "https://media.istockphoto.com/id/1293763250/photo/cute-kitten-licking-glass-table-with-copy-space.jpg?s=1024x1024&w=is&k=20&c=h6jpFt9v3fVB1ggzPJnvD54yQRjc9Hv8rbDK8BP0IJo="
  "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRUrZ4dVINFjQW2G7KXZeIPpE1cVGEJxlAJtT3uVl-5cQ&s=10"
  "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcRzVs9SaII0uVpjAK-8mzoxw8eGdEXdEhSURvbZ5uec1Q&s"
)

ENDPOINT="localhost:8000/v1/chat/completions"
MODEL="Qwen/Qwen3-VL-235B-A22B-Instruct-FP8"

ITERATIONS="${1:-1}"

for ((i=1; i<=ITERATIONS; i++)); do
  echo "########## Iteration $i / $ITERATIONS ##########"
  for url in "${URLS[@]}"; do
    if [ ${#url} -gt 100 ]; then
      display_url="${url:0:100}..."
    else
      display_url="$url"
    fi
    echo "=== Request for image: $display_url ==="
    curl -i "$ENDPOINT" \
      -H 'Content-Type: application/json' \
      -d "$(cat <<EOF
{
  "model": "$MODEL",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "What animal is shown in this image?"},
        {"type": "image_url", "image_url": {"url": "$url"}}
      ]
    }
  ],
  "max_tokens": 50,
  "ignore_eos": true,
  "stream": false
}
EOF
)"
    echo
  done
done
