"""Real tokenizer integration: entirely local assets, no model or network calls."""
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("with_template", [True, False])
async def test_real_fast_tokenizer_sizes_full_chat_from_local_files(tmp_path, with_template):
    tokenizers = pytest.importorskip("tokenizers")
    transformers = pytest.importorskip("transformers")
    from giraffe.concurrent_load import prompt
    from giraffe.local_tokenizer import prepare_messages

    backend = tokenizers.Tokenizer(tokenizers.models.BPE(unk_token="[UNK]"))
    backend.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    backend.decoder = tokenizers.decoders.ByteLevel()
    backend.train_from_iterator(
        [prompt(8192, "short_answer", nonce="0123456789abcdef" * 2)],
        trainer=tokenizers.trainers.BpeTrainer(vocab_size=300, special_tokens=["[UNK]", "[BOS]", "[EOS]"]),
    )
    tokenizer = transformers.PreTrainedTokenizerFast(tokenizer_object=backend,
        unk_token="[UNK]", bos_token="[BOS]", eos_token="[EOS]")
    if with_template:
        tokenizer.chat_template = "[BOS]{% for message in messages %}{{ message['role'] }}:{{ message['content'] }}[EOS]{% endfor %}{% if add_generation_prompt %}assistant:{% endif %}"
    tokenizer.save_pretrained(tmp_path)
    target = SimpleNamespace(name="local", model="served-alias", tokenizer=str(tmp_path))
    events = []
    run = SimpleNamespace(deadline=time.monotonic() + 30,
        preparation=lambda *args, **kwargs: events.append((args, kwargs)))
    result = await prepare_messages(run, target, "load_preparation", lambda chars: [
        {"role": "user", "content": prompt(chars, "short_answer", nonce="0123456789abcdef" * 2)}],
        128, tolerance=.01)
    assert result["status"] == "calibrated", result
    actual = (tokenizer.apply_chat_template(result["messages"], tokenize=True, add_generation_prompt=True, return_dict=False)
              if with_template else tokenizer.encode(result["messages"][0]["content"], add_special_tokens=False))
    assert result["input_tokens"] == len(actual)
    assert abs(len(actual) - 128) <= 1.28
    assert result["tokenizer"] == str(tmp_path) and result["tokenizer_source"] == "configured"
    assert result["local_files_only"] and result["chat_template"] == with_template
    assert result["token_count_scope"] == ("chat_template" if with_template else "message_content")
    assert not result["request_ids"]
    assert events[-1][0][3] == "completed"
