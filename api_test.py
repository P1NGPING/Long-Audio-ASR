import sys

from openai import OpenAI

from asr_transcriber import load_config


def test_api():
    config = load_config()
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "format"

    if target == "asr":
        api_key = config["asr_api_key"]
        base_url = config["asr_base_url"]
        model = config["asr_model"]
        label = "ASR"
    else:
        api_key = config["format_api_key"]
        base_url = config["format_base_url"]
        model = config["format_model"]
        label = "Markdown 修整"

    client = OpenAI(api_key=api_key, base_url=base_url)

    print(f"正在测试: {label}")
    print(f"正在尝试连接服务器: {base_url}")
    print(f"使用模型: {model}")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "你好，这是一个API连通性测试。如果你收到了这条消息，请只回复“连接成功”四个字。"}
            ],
        )
        print("\n测试成功")
        print(f"模型回复: {response.choices[0].message.content}")
    except Exception as e:
        print("\n测试失败")
        print(f"错误信息: {e}")


if __name__ == "__main__":
    test_api()
