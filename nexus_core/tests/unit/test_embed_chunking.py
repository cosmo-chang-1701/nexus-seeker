import discord
from cogs.embed_builder import get_embed_length, chunk_embeds


def test_get_embed_length():
    # 1. Create an embed with title, description, footer, author, and fields
    embed = discord.Embed(title="Hello", description="World")
    embed.set_author(name="AuthorName")
    embed.set_footer(text="FooterText")
    embed.add_field(name="Field1", value="Value1")
    embed.add_field(name="Field2", value="Value2")

    # Sum of lengths:
    # Hello: 5
    # World: 5
    # AuthorName: 10
    # FooterText: 10
    # Field1: 6, Value1: 6 -> 12
    # Field2: 6, Value2: 6 -> 12
    # Total = 5 + 5 + 10 + 10 + 12 + 12 = 54
    assert get_embed_length(embed) == 54

    # 2. Test empty embed
    empty_embed = discord.Embed()
    assert get_embed_length(empty_embed) == 0


def test_chunk_embeds_by_size():
    embeds = [
        discord.Embed(description="A" * 2000),  # 2000
        discord.Embed(description="B" * 2000),  # 2000
        discord.Embed(description="C" * 2000),  # 2000
    ]
    # Under max_size=3000, 2000 + 2000 = 4000 > 3000, so each should be in its own chunk
    chunks = chunk_embeds(embeds, max_size=3000)
    assert len(chunks) == 3
    assert len(chunks[0]) == 1
    assert len(chunks[1]) == 1
    assert len(chunks[2]) == 1

    # Under max_size=5000, first two (4000) fit in one chunk, the third (2000) in the next
    chunks2 = chunk_embeds(embeds, max_size=5000)
    assert len(chunks2) == 2
    assert len(chunks2[0]) == 2
    assert len(chunks2[1]) == 1


def test_chunk_embeds_by_count():
    embeds = [discord.Embed(description="A") for _ in range(15)]
    # Under max_count=5, 15 embeds should be split into 3 chunks of 5
    chunks = chunk_embeds(embeds, max_count=5)
    assert len(chunks) == 3
    assert all(len(c) == 5 for c in chunks)


def test_single_oversized_embed():
    embeds = [
        discord.Embed(description="A" * 6000),  # 6000 (oversized)
        discord.Embed(description="B" * 100),  # 100
    ]
    # An oversized embed should still be chunked safely, and the next fits in the subsequent chunk
    chunks = chunk_embeds(embeds, max_size=5000)
    assert len(chunks) == 2
    assert len(chunks[0]) == 1
    assert len(chunks[1]) == 1
