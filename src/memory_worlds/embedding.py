"""Bounded embedding requests for Graphiti's OpenAI-compatible transport."""

from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig


class BatchingOpenAIEmbedder(OpenAIEmbedder):
    """Split bulk work for endpoints that accept only one input per request."""

    def __init__(self, config: OpenAIEmbedderConfig, *, batch_size: int = 1, client=None):
        if type(batch_size) is not int or not 1 <= batch_size <= 2048:
            raise ValueError("embedding batch size must be an integer between 1 and 2048")
        super().__init__(config=config, client=client)
        self.batch_size = batch_size

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        embeddings = []
        for offset in range(0, len(input_data_list), self.batch_size):
            batch = input_data_list[offset : offset + self.batch_size]
            response = await self.client.embeddings.create(
                input=batch, model=self.config.embedding_model
            )
            ordered = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in ordered] != list(range(len(batch))):
                raise ValueError("embedding response does not match the requested input batch")
            if any(len(item.embedding) < self.config.embedding_dim for item in ordered):
                raise ValueError("embedding response has fewer dimensions than configured")
            embeddings.extend(item.embedding[: self.config.embedding_dim] for item in ordered)
        return embeddings
