"""Chunking objects not specific to a particular chunking strategy."""

from __future__ import annotations

import collections
import copy
from typing import Any, Callable, DefaultDict, Iterable, Iterator, cast

import regex
from typing_extensions import Self, TypeAlias

from unstructured.common.html_table import HtmlCell, HtmlRow, HtmlTable
from unstructured.documents.elements import (
    CompositeElement,
    ConsolidationStrategy,
    Element,
    ElementMetadata,
    Table,
    TableChunk,
    Title,
)
from unstructured.utils import lazyproperty

# ================================================================================================
# MODEL
# ================================================================================================

CHUNK_MAX_CHARS_DEFAULT: int = 500
"""Hard-max chunk-length when no explicit value specified in `max_characters` argument.

Provided for reference only, for example so the ingest CLI can advertise the default value in its
UI. External chunking-related functions (e.g. in ingest or decorators) should use
`max_characters: int | None = None` and not apply this default themselves. Only
`ChunkingOptions.max_characters` should apply a default value.
"""

CHUNK_MULTI_PAGE_DEFAULT: bool = True
"""When False, respect page-boundaries (no two elements from different page in same chunk).

Only operative for "by_title" chunking strategy.
"""

BoundaryPredicate: TypeAlias = Callable[[Element], bool]
"""Detects when element represents crossing a semantic boundary like section or page."""

TextAndHtml: TypeAlias = tuple[str, str]


# ================================================================================================
# CHUNKING OPTIONS
# ================================================================================================


class ChunkingOptions:
    """Specifies parameters of optional chunking behaviors.

    Parameters
    ----------
    max_characters
        Hard-maximum text-length of chunk. A chunk longer than this will be split mid-text and be
        emitted as two or more chunks.
    new_after_n_chars
        Preferred approximate chunk size. A chunk composed of elements totalling this size or
        greater is considered "full" and will not be enlarged by adding another element, even if it
        will fit within the remaining `max_characters` for that chunk. Defaults to `max_characters`
        when not specified, which effectively disables this behavior. Specifying 0 for this
        argument causes each element to appear in a chunk by itself (although an element with text
        longer than `max_characters` will be still be split into two or more chunks).
    combine_text_under_n_chars
        Provides a way to "recombine" small chunks formed by breaking on a semantic boundary. Only
        relevant for a chunking strategy that specifies higher-level semantic boundaries to be
        respected, like "section" or "page". Recursively combines two adjacent pre-chunks when the
        first pre-chunk is smaller than this threshold. "Recursively" here means the resulting
        pre-chunk can be combined with the next pre-chunk if it is still under the length threshold.
        Defaults to `max_characters` which combines chunks whenever space allows. Specifying 0 for
        this argument suppresses combining of small chunks. Note this value is "capped" at the
        `new_after_n_chars` value since a value higher than that would not change this parameter's
        effect.
    overlap
        Specifies the length of a string ("tail") to be drawn from each chunk and prefixed to the
        next chunk as a context-preserving mechanism. By default, this only applies to split-chunks
        where an oversized element is divided into multiple chunks by text-splitting.
    overlap_all
        Default: `False`. When `True`, apply overlap between "normal" chunks formed from whole
        elements and not subject to text-splitting. Use this with caution as it entails a certain
        level of "pollution" of otherwise clean semantic chunk boundaries.
    text_splitting_separators
        A sequence of strings like `("\n", " ")` to be used as target separators during
        text-splitting. Text-splitting only applies to splitting an oversized element into two or
        more chunks. These separators are tried in the specified order until one is found in the
        string to be split. The default separator is `""` which matches between any two characters.
        This separator should not be specified in this sequence because it is always the separator
        of last-resort. Note that because the separator is removed during text-splitting, only
        whitespace character sequences are suitable.
    """

    def __init__(self, **kwargs: Any):
        self._kwargs = kwargs

    @classmethod
    def new(cls, **kwargs: Any) -> Self:
        """Return instance or raises `ValueError` on invalid arguments like overlap > max_chars."""
        self = cls(**kwargs)
        self._validate()
        return self

    @lazyproperty
    def boundary_predicates(self) -> tuple[BoundaryPredicate, ...]:
        """The semantic-boundary detectors to be applied to break pre-chunks.

        Overridden by sub-typs to provide semantic-boundary isolation behaviors.
        """
        return ()

    @lazyproperty
    def combine_text_under_n_chars(self) -> int:
        """Combine two consecutive text pre-chunks if first is smaller than this and both will fit.

        Default applied here is `0` which essentially disables chunk combining. Must be overridden
        by subclass where combining behavior is supported.
        """
        arg_value = self._kwargs.get("combine_text_under_n_chars")
        return arg_value if arg_value is not None else 0

    @lazyproperty
    def hard_max(self) -> int:
        """The maximum size for a chunk.

        A pre-chunk will only exceed this size when it contains exactly one element which by itself
        exceeds this size. Such a pre-chunk is subject to mid-text splitting later in the chunking
        process.
        """
        arg_value = self._kwargs.get("max_characters")
        return arg_value if arg_value is not None else CHUNK_MAX_CHARS_DEFAULT

    @lazyproperty
    def include_orig_elements(self) -> bool:
        """When True, add original elements from pre-chunk to `.metadata.orig_elements` of chunk.

        Default value is `True`.
        """
        arg_value = self._kwargs.get("include_orig_elements")
        return True if arg_value is None else bool(arg_value)

    @lazyproperty
    def inter_chunk_overlap(self) -> int:
        """Characters of overlap to add between chunks.

        This applies only to boundaries between chunks formed from whole elements and not to
        text-splitting boundaries that arise from splitting an oversized element.
        """
        overlap_all_arg = self._kwargs.get("overlap_all")
        return self.overlap if overlap_all_arg else 0

    @lazyproperty
    def overlap(self) -> int:
        """The number of characters to overlap text when splitting chunks mid-text.

        The actual overlap will not exceed this number of characters but may be less as required to
        respect splitting-character boundaries.
        """
        overlap_arg = self._kwargs.get("overlap")
        return overlap_arg or 0

    @lazyproperty
    def soft_max(self) -> int:
        """A pre-chunk of this size or greater is considered full.

        Note that while a value of `0` is valid, it essentially disables chunking by putting
        each element into its own chunk.
        """
        hard_max = self.hard_max
        new_after_n_chars_arg = self._kwargs.get("new_after_n_chars")

        # -- default value is == max_characters --
        if new_after_n_chars_arg is None:
            return hard_max

        # -- new_after_n_chars > max_characters behaves the same as ==max_characters --
        if new_after_n_chars_arg > hard_max:
            return hard_max

        # -- otherwise, give them what they asked for --
        return new_after_n_chars_arg

    @lazyproperty
    def split(self) -> Callable[[str], tuple[str, str]]:
        """A text-splitting function suitable for splitting the text of an oversized pre-chunk.

        The function is pre-configured with the chosen chunking window size and any other applicable
        options specified by the caller as part of this chunking-options instance.
        """
        return _TextSplitter(self)

    @lazyproperty
    def text_separator(self) -> str:
        """The string to insert between elements when concatenating their text for a chunk.

        Right now this is just "\n\n" (a blank line in plain text), but having this here rather
        than as a module-level constant provides a way for us to easily make it user-configurable
        in future if we want to.
        """
        return "\n\n"

    @lazyproperty
    def text_splitting_separators(self) -> tuple[str, ...]:
        """Sequence of text-splitting target strings to be used in order of preference."""
        text_splitting_separators_arg = self._kwargs.get("text_splitting_separators")
        return (
            ("\n", " ")
            if text_splitting_separators_arg is None
            else tuple(text_splitting_separators_arg)
        )

    def _validate(self) -> None:
        """Raise ValueError if requestion option-set is invalid."""
        max_characters = self.hard_max
        # -- chunking window must have positive length --
        if max_characters <= 0:
            raise ValueError(f"'max_characters' argument must be > 0," f" got {max_characters}")

        # -- a negative value for `new_after_n_chars` is assumed to be a mistake the caller will
        # -- want to know about
        new_after_n_chars = self._kwargs.get("new_after_n_chars")
        if new_after_n_chars is not None and new_after_n_chars < 0:
            raise ValueError(
                f"'new_after_n_chars' argument must be >= 0," f" got {new_after_n_chars}"
            )

        # -- overlap must be less than max-chars or the chunk text will never be consumed --
        if self.overlap >= max_characters:
            raise ValueError(
                f"'overlap' argument must be less than `max_characters`,"
                f" got {self.overlap} >= {max_characters}"
            )


# ================================================================================================
# PRE-CHUNKER
# ================================================================================================


class PreChunker:
    """Gathers sequential elements into pre-chunks as length constraints allow.

    The pre-chunker's responsibilities are:

    - **Segregate semantic units.** Identify semantic unit boundaries and segregate elements on
      either side of those boundaries into different sections. In this case, the primary indicator
      of a semantic boundary is a `Title` element. A page-break (change in page-number) is also a
      semantic boundary when `multipage_sections` is `False`.

    - **Minimize chunk count for each semantic unit.** Group the elements within a semantic unit
      into sections as big as possible without exceeding the chunk window size.

    - **Minimize chunks that must be split mid-text.** Precompute the text length of each section
      and only produce a section that exceeds the chunk window size when there is a single element
      with text longer than that window.

    A Table element is placed into a section by itself. CheckBox elements are dropped.

    The "by-title" strategy specifies breaking on section boundaries; a `Title` element indicates
    a new "section", hence the "by-title" designation.
    """

    def __init__(self, elements: Iterable[Element], opts: ChunkingOptions):
        self._elements = elements
        self._opts = opts

    @classmethod
    def iter_pre_chunks(
        cls, elements: Iterable[Element], opts: ChunkingOptions
    ) -> Iterator[PreChunk]:
        """Generate pre-chunks from the element-stream provided on construction."""
        return cls(elements, opts)._iter_pre_chunks()

    def _iter_pre_chunks(self) -> Iterator[PreChunk]:
        """Generate pre-chunks from the element-stream provided on construction.

        A *pre-chunk* is the largest sub-sequence of elements that will both fit within the
        chunking window and respects the semantic boundary rules of the chunking strategy. When a
        single element exceeds the chunking window size it is placed in a pre-chunk by itself and
        is subject to mid-text splitting in the second phase of the chunking process.
        """
        pre_chunk_builder = PreChunkBuilder(self._opts)

        for element in self._elements:
            # -- start new pre-chunk when necessary to uphold segregation guarantees --
            if (
                # -- start new pre-chunk when necessary to uphold segregation guarantees --
                self._is_in_new_semantic_unit(element)
                # -- or when next element won't fit --
                or not pre_chunk_builder.will_fit(element)
            ):
                yield from pre_chunk_builder.flush()

            # -- add this element to the work-in-progress (WIP) pre-chunk --
            pre_chunk_builder.add_element(element)

        # -- flush "tail" pre-chunk, any partially-filled pre-chunk after last element is
        # -- processed
        yield from pre_chunk_builder.flush()

    @lazyproperty
    def _boundary_predicates(self) -> tuple[BoundaryPredicate, ...]:
        """The semantic-boundary detectors to be applied to break pre-chunks."""
        return self._opts.boundary_predicates

    def _is_in_new_semantic_unit(self, element: Element) -> bool:
        """True when `element` begins a new semantic unit such as a section or page."""
        # -- all detectors need to be called to update state and avoid double counting
        # -- boundaries that happen to coincide, like Table and new section on same element.
        # -- Using `any()` would short-circuit on first True.
        semantic_boundaries = [pred(element) for pred in self._boundary_predicates]
        return any(semantic_boundaries)


class PreChunkBuilder:
    """An element accumulator suitable for incrementally forming a pre-chunk.

    Provides the trial method `.will_fit()` a pre-chunker can use to determine whether it should add
    the next element in the element stream.

    `.flush()` is used to build a PreChunk object from the accumulated elements. This method
    returns an iterator that generates zero-or-one `PreChunk` object and is used like so:

        yield from builder.flush()

    If no elements have been accumulated, no `PreChunk` instance is generated. Flushing the builder
    clears the elements it contains so it is ready to build the next pre-chunk.
    """

    def __init__(self, opts: ChunkingOptions) -> None:
        self._opts = opts
        self._separator_len = len(opts.text_separator)
        self._elements: list[Element] = []

        # -- overlap is only between pre-chunks so starts empty --
        self._overlap_prefix: str = ""
        # -- only includes non-empty element text, e.g. PageBreak.text=="" is not included --
        self._text_segments: list[str] = []
        # -- combined length of text-segments, not including separators --
        self._text_len: int = 0

    def add_element(self, element: Element) -> None:
        """Add `element` to this section."""
        self._elements.append(element)
        if element.text:
            self._text_segments.append(element.text)
            self._text_len += len(element.text)

    def flush(self) -> Iterator[PreChunk]:
        """Generate zero-or-one `PreChunk` object and clear the accumulator.

        Suitable for use to emit a PreChunk when the maximum size has been reached or a semantic
        boundary has been reached. Also to clear out a terminal pre-chunk at the end of an element
        stream.
        """
        elements = self._elements

        if not elements:
            return

        # -- copy element list, don't use original or it may change contents as builder proceeds --
        pre_chunk = PreChunk(elements, self._overlap_prefix, self._opts)
        # -- clear builder before yield so we're not sensitive to the timing of how/when this
        # -- iterator is exhausted and can add elements for the next pre-chunk immediately.
        self._reset_state(pre_chunk.overlap_tail)
        yield pre_chunk

    def will_fit(self, element: Element) -> bool:
        """True when `element` can be added to this prechunk without violating its limits.

        There are several limits:
        - A `Table` element will never fit with any other element. It will only fit in an empty
          pre-chunk.
        - No element will fit in a pre-chunk that already contains a `Table` element.
        - A text-element will not fit in a pre-chunk that already exceeds the soft-max
          (aka. new_after_n_chars).
        - A text-element will not fit when together with the elements already present it would
          exceed the hard-max (aka. max_characters).
        """
        # -- an empty pre-chunk will accept any element (including an oversized-element) --
        if len(self._elements) == 0:
            return True
        # -- a pre-chunk that already exceeds the soft-max is considered "full" --
        if self._text_length > self._opts.soft_max:
            return False
        # -- don't add an element if it would increase total size beyond the hard-max --
        return not self._remaining_space < len(element.text or "")

    @property
    def _remaining_space(self) -> int:
        """Maximum text-length of an element that can be added without exceeding maxlen."""
        # -- include length of trailing separator that will go before next element text --
        separators_len = self._separator_len * len(self._text_segments)
        return self._opts.hard_max - self._text_len - separators_len

    def _reset_state(self, overlap_prefix: str) -> None:
        """Set working-state values back to "empty", ready to accumulate next pre-chunk."""
        self._overlap_prefix = overlap_prefix
        self._elements.clear()
        self._text_segments = [overlap_prefix] if overlap_prefix else []
        self._text_len = len(overlap_prefix)

    @property
    def _text_length(self) -> int:
        """Length of the text in this pre-chunk.

        This value represents the chunk-size that would result if this pre-chunk was flushed in its
        current state. In particular, it does not include the length of a trailing separator (since
        that would only appear if an additional element was added).

        Not suitable for judging remaining space, use `.remaining_space` for that value.
        """
        # -- number of text separators present in joined text of elements. This includes only
        # -- separators *between* text segments, not one at the end. Note there are zero separators
        # -- for both 0 and 1 text-segments.
        n = len(self._text_segments)
        separator_count = n - 1 if n else 0
        return self._text_len + (separator_count * self._separator_len)


# ================================================================================================
# PRE-CHUNK
# ================================================================================================


class PreChunk:
    """Sequence of elements staged to form a single chunk.

    This object is purposely immutable.
    """

    def __init__(
        self, elements: Iterable[Element], overlap_prefix: str, opts: ChunkingOptions
    ) -> None:
        self._elements = list(elements)
        self._overlap_prefix = overlap_prefix
        self._opts = opts

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, PreChunk):
            return False
        return self._overlap_prefix == other._overlap_prefix and self._elements == other._elements

    def can_combine(self, pre_chunk: PreChunk) -> bool:
        """True when `pre_chunk` can be combined with this one without exceeding size limits."""
        if len(self._text) >= self._opts.combine_text_under_n_chars:
            return False
        # -- avoid duplicating length computations by doing a trial-combine which is just as
        # -- efficient and definitely more robust than hoping two different computations of combined
        # -- length continue to get the same answer as the code evolves. Only possible because
        # -- `.combine()` is non-mutating.
        combined_len = len(self.combine(pre_chunk)._text)

        return combined_len <= self._opts.hard_max

    def combine(self, other_pre_chunk: PreChunk) -> PreChunk:
        """Return new `PreChunk` that combines this and `other_pre_chunk`."""
        # -- combined pre-chunk gets the overlap-prefix of the first pre-chunk. The second overlap
        # -- is automatically incorporated at the end of the first chunk, where it originated.
        return PreChunk(
            self._elements + other_pre_chunk._elements,
            overlap_prefix=self._overlap_prefix,
            opts=self._opts,
        )

    def iter_chunks(self) -> Iterator[CompositeElement | Table | TableChunk]:
        """Form this pre-chunk into one or more chunk elements maxlen or smaller.

        When the total size of the pre-chunk will fit in the chunking window, a single chunk it
        emitted. When this prechunk contains an oversized element (always isolated), it is split
        into two or more chunks that each fit the chunking window.
        """

        # -- a one-table-only pre-chunk is handled specially, by `TablePreChunk`, mainly because
        # -- it may need to be split into multiple `TableChunk` elements and that operation is
        # -- quite specialized.
        if len(self._elements) == 1 and isinstance(self._elements[0], Table):
            yield from _TableChunker.iter_chunks(
                self._elements[0], self._overlap_prefix, self._opts
            )
        else:
            yield from _Chunker.iter_chunks(self._elements, self._text, self._opts)

    @lazyproperty
    def overlap_tail(self) -> str:
        """The portion of this chunk's text to be repeated as a prefix in the next chunk.

        This value is the empty-string ("") when either the `.overlap` length option is `0` or
        `.overlap_all` is `False`. When there is a text value, it is stripped of both leading and
        trailing whitespace.
        """
        overlap = self._opts.inter_chunk_overlap
        return self._text[-overlap:].strip() if overlap else ""

    def _iter_text_segments(self) -> Iterator[str]:
        """Generate overlap text and each element text segment in order.

        Empty text segments are not included.
        """
        if self._overlap_prefix:
            yield self._overlap_prefix
        for e in self._elements:
            if e.text and len(e.text):
                text = " ".join(e.text.strip().split())
                if text:
                    yield text

    @lazyproperty
    def _text(self) -> str:
        """The concatenated text of all elements in this pre-chunk, including any overlap.

        Whitespace is normalized to a single space. The text of each element is separated from
        that of the next by a blank line ("\n\n").
        """
        return self._opts.text_separator.join(self._iter_text_segments())


# ================================================================================================
# CHUNKING HELPER/SPLITTERS
# ================================================================================================


class _Chunker:
    """Forms chunks from a pre-chunk other than one containing only a `Table`.

    Produces zero-or-more `CompositeElement` objects.
    """

    def __init__(self, elements: Iterable[Element], text: str, opts: ChunkingOptions) -> None:
        self._elements = list(elements)
        self._text = text
        self._opts = opts

    @classmethod
    def iter_chunks(
        cls, elements: Iterable[Element], text: str, opts: ChunkingOptions
    ) -> Iterator[CompositeElement]:
        """Form zero or more chunks from `elements`.

        One `CompositeElement` is produced when all `elements` will fit. Otherwise there is a
        single `Text`-subtype element and chunks are formed by splitting.
        """
        return cls(elements, text, opts)._iter_chunks()

    def _iter_chunks(self) -> Iterator[CompositeElement]:
        """Form zero or more chunks from `elements`."""
        # -- a pre-chunk containing no text (maybe only a PageBreak element for example) does not
        # -- generate any chunks.
        if not self._text:
            return

        # -- `split()` is the text-splitting function used to split an oversized element --
        split = self._opts.split

        # -- emit first chunk --
        s, remainder = split(self._text)
        yield CompositeElement(text=s, metadata=self._consolidated_metadata)

        # -- an oversized pre-chunk will have a remainder, split that up into additional chunks.
        # -- Note these get continuation_metadata which includes is_continuation=True.
        while remainder:
            s, remainder = split(remainder)
            yield CompositeElement(text=s, metadata=self._continuation_metadata)

    @lazyproperty
    def _all_metadata_values(self) -> dict[str, list[Any]]:
        """Collection of all populated metadata values across elements.

        The resulting dict has one key for each `ElementMetadata` field that had a non-None value in
        at least one of the elements in this pre-chunk. The value of that key is a list of all those
        populated values, in element order, for example:

            {
                "filename": ["sample.docx", "sample.docx"],
                "languages": [["lat"], ["lat", "eng"]]
                ...
            }

        This preprocessing step provides the input for a specified consolidation strategy that will
        resolve the list of values for each field to a single consolidated value.
        """

        def iter_populated_fields(metadata: ElementMetadata) -> Iterator[tuple[str, Any]]:
            """(field_name, value) pair for each non-None field in single `ElementMetadata`."""
            return (
                (field_name, value)
                for field_name, value in metadata.known_fields.items()
                if value is not None
            )

        field_values: DefaultDict[str, list[Any]] = collections.defaultdict(list)

        # -- collect all non-None field values in a list for each field, in element-order --
        for e in self._elements:
            for field_name, value in iter_populated_fields(e.metadata):
                field_values[field_name].append(value)

        return dict(field_values)

    @lazyproperty
    def _consolidated_metadata(self) -> ElementMetadata:
        """Metadata applicable to this pre-chunk as a single chunk.

        Formed by applying consolidation rules to all metadata fields across the elements of this
        pre-chunk.

        For the sake of consistency, the same rules are applied (for example, for dropping values)
        to a single-element pre-chunk too, even though metadata for such a pre-chunk is already
        "consolidated".
        """
        consolidated_metadata = ElementMetadata(**self._meta_kwargs)
        if self._opts.include_orig_elements:
            consolidated_metadata.orig_elements = self._orig_elements
        return consolidated_metadata

    @lazyproperty
    def _continuation_metadata(self) -> ElementMetadata:
        """Metadata applicable to the second and later text-split chunks of the pre-chunk.

        The same metadata as the first text-split chunk but includes `.is_continuation = True`.
        Unused for non-oversized pre-chunks since those are not subject to text-splitting.
        """
        # -- we need to make a copy, otherwise adding a field would also change metadata value
        # -- already assigned to another chunk (e.g. the first text-split chunk). Deep-copy is not
        # -- required though since we're not changing any collection fields.
        continuation_metadata = copy.copy(self._consolidated_metadata)
        continuation_metadata.is_continuation = True
        return continuation_metadata

    @lazyproperty
    def _meta_kwargs(self) -> dict[str, Any]:
        """The consolidated metadata values as a dict suitable for constructing ElementMetadata.

        This is where consolidation strategies are actually applied. The output is suitable for use
        in constructing an `ElementMetadata` object like `ElementMetadata(**self._meta_kwargs)`.
        """
        CS = ConsolidationStrategy
        field_consolidation_strategies = ConsolidationStrategy.field_consolidation_strategies()

        def iter_kwarg_pairs() -> Iterator[tuple[str, Any]]:
            """Generate (field-name, value) pairs for each field in consolidated metadata."""
            for field_name, values in self._all_metadata_values.items():
                strategy = field_consolidation_strategies.get(field_name)
                if strategy is CS.FIRST:
                    yield field_name, values[0]
                # -- concatenate lists from each element that had one, in order --
                elif strategy is CS.LIST_CONCATENATE:
                    yield field_name, sum(values, cast("list[Any]", []))
                # -- union lists from each element, preserving order of appearance --
                elif strategy is CS.LIST_UNIQUE:
                    # -- Python 3.7+ maintains dict insertion order --
                    ordered_unique_keys = {key: None for val_list in values for key in val_list}
                    yield field_name, list(ordered_unique_keys.keys())
                elif strategy is CS.STRING_CONCATENATE:
                    yield field_name, " ".join(val.strip() for val in values)
                elif strategy is CS.DROP:
                    continue
                else:  # pragma: no cover
                    # -- not likely to hit this since we have a test in `text_elements.py` that
                    # -- ensures every ElementMetadata fields has an assigned strategy.
                    raise NotImplementedError(
                        f"metadata field {repr(field_name)} has no defined consolidation strategy"
                    )

        return dict(iter_kwarg_pairs())

    @lazyproperty
    def _orig_elements(self) -> list[Element]:
        """The `.metadata.orig_elements` value for chunks formed from this pre-chunk."""

        def iter_orig_elements():
            for e in self._elements:
                if e.metadata.orig_elements is None:
                    yield e
                    continue
                # -- make copy of any element we're going to mutate because these elements don't
                # -- belong to us (the user may have downstream purposes for them).
                orig_element = copy.copy(e)
                # -- prevent recursive .orig_elements when element is a chunk (has orig-elements of
                # -- its own)
                orig_element.metadata.orig_elements = None
                yield orig_element

        return list(iter_orig_elements())


class _TableChunker:
    """Responsible for forming chunks, especially splits, from a single-table pre-chunk.

    Table splitting is specialized because we recursively split on an even row, cell, text
    boundary. This object encapsulate those details.
    """

    def __init__(self, table: Table, overlap_prefix: str, opts: ChunkingOptions) -> None:
        self._table = table
        self._overlap_prefix = overlap_prefix
        self._opts = opts

    @classmethod
    def iter_chunks(
        cls, table: Table, overlap_prefix: str, opts: ChunkingOptions
    ) -> Iterator[Table | TableChunk]:
        """Split this pre-chunk into `Table` or `TableChunk` objects maxlen or smaller."""
        return cls(table, overlap_prefix, opts)._iter_chunks()

    def _iter_chunks(self) -> Iterator[Table | TableChunk]:
        """Split this pre-chunk into `Table` or `TableChunk` objects maxlen or smaller."""
        # -- A table with no non-whitespace text produces no chunks --
        if not self._table_text:
            return

        # -- only text-split a table when it's longer than the chunking window --
        maxlen = self._opts.hard_max
        if len(self._text_with_overlap) <= maxlen and len(self._html) <= maxlen:
            # -- use the compactified html for .text_as_html, even though we're not splitting --
            metadata = self._metadata
            metadata.text_as_html = self._html or None
            # -- note the overlap-prefix is prepended to its text --
            yield Table(text=self._text_with_overlap, metadata=metadata)
            return

        # -- When there's no HTML, split it like a normal element. Also fall back to text-only
        # -- chunks when `max_characters` is less than 50. `.text_as_html` metadata is impractical
        # -- for a chunking window that small because the 33 characters of HTML overhead for each
        # -- chunk (`<table><tr><td>...</td></tr></table>`) would produce a very large number of
        # -- very small chunks.
        if not self._html or self._opts.hard_max < 50:
            yield from self._iter_text_only_table_chunks()
            return

        # -- otherwise, form splits with "synchronized" text and html --
        yield from self._iter_text_and_html_table_chunks()

    @lazyproperty
    def _html(self) -> str:
        """The compactified HTML for this table when it has text-as-HTML.

        The empty string when table-structure has not been captured, perhaps because
        `infer_table_structure` was set `False` in the partitioning call.
        """
        if not (html_table := self._html_table):
            return ""

        return html_table.html

    @lazyproperty
    def _html_table(self) -> HtmlTable | None:
        """The `lxml` HTML element object for this table.

        `None` when the `Table` element has no `.metadata.text_as_html`.
        """
        if (text_as_html := self._table.metadata.text_as_html) is None:
            return None

        text_as_html = text_as_html.strip()
        if not text_as_html:  # pragma: no cover
            return None

        return HtmlTable.from_html_text(text_as_html)

    def _iter_text_and_html_table_chunks(self) -> Iterator[TableChunk]:
        """Split table into chunks where HTML corresponds exactly to text.

        `.metadata.text_as_html` for each chunk is a parsable `<table>` HTML fragment.
        """
        if (html_table := self._html_table) is None:  # pragma: no cover
            raise ValueError("this method is undefined for a table having no .text_as_html")

        is_continuation = False

        for text, html in _HtmlTableSplitter.iter_subtables(html_table, self._opts):
            metadata = self._metadata
            metadata.text_as_html = html
            # -- second and later chunks get `.metadata.is_continuation = True` --
            metadata.is_continuation = is_continuation or None
            is_continuation = True

            yield TableChunk(text=text, metadata=metadata)

    def _iter_text_only_table_chunks(self) -> Iterator[TableChunk]:
        """Split oversized text-only table (no text-as-html) into chunks.

        `.metadata.text_as_html` is optional, not included when `infer_table_structure` is
        `False`.
        """
        text_remainder = self._text_with_overlap
        split = self._opts.split
        is_continuation = False

        while text_remainder:
            # -- split off the next chunk-worth of characters into a TableChunk --
            chunk_text, text_remainder = split(text_remainder)
            metadata = self._metadata
            # -- second and later chunks get `.metadata.is_continuation = True` --
            metadata.is_continuation = is_continuation or None
            is_continuation = True

            yield TableChunk(text=chunk_text, metadata=metadata)

    @property
    def _metadata(self) -> ElementMetadata:
        """The base `.metadata` value for chunks formed from this pre-chunk.

        The term "base" here means that other metadata fields will be added, depending on the
        chunk. In particular, `.metadata.text_as_html` will be different for each text-split chunk
        and `.metadata.is_continuation` must be added for second-and-later text-split chunks.

        Note this is a fresh copy of the metadata on each call since it will need to be mutated
        differently for each chunk formed from this pre-chunk.
        """
        CS = ConsolidationStrategy
        metadata = copy.deepcopy(self._table.metadata)

        # -- drop metadata fields not appropriate for chunks, in particular
        # -- parent_id's will not reliably point to an existing element
        drop_field_names = [
            field_name
            for field_name, strategy in CS.field_consolidation_strategies().items()
            if strategy is CS.DROP
        ]
        for field_name in drop_field_names:
            setattr(metadata, field_name, None)

        if self._opts.include_orig_elements:
            metadata.orig_elements = self._orig_elements
        return metadata

    @lazyproperty
    def _orig_elements(self) -> list[Element]:
        """The `.metadata.orig_elements` value for chunks formed from this pre-chunk.

        Note this is not just the `Table` element, it must be adjusted to strip out any
        `.metadata.orig_elements` value it may have when it is itself a chunk and not a direct
        product of partitioning.
        """
        # -- make a copy because we're going to mutate the `Table` element and it doesn't belong to
        # -- us (the user may have downstream purposes for it).
        orig_table = copy.deepcopy(self._table)
        # -- prevent recursive .orig_elements when `Table` element is a chunk --
        orig_table.metadata.orig_elements = None
        return [orig_table]

    @lazyproperty
    def _table_text(self) -> str:
        """The text in this table, not including any overlap-prefix or extra whitespace."""
        if not self._table.text:
            return ""
        return " ".join(self._table.text.split())

    @lazyproperty
    def _text_with_overlap(self) -> str:
        """The text for this chunk, including the overlap-prefix when present."""
        overlap_prefix = self._overlap_prefix
        table_text = "" if not self._table.text else self._table.text.strip()
        # -- use row-separator between overlap and table-text --
        return overlap_prefix + "\n" + table_text if overlap_prefix else table_text


# ================================================================================================
# HTML SPLITTERS
# ================================================================================================


class _HtmlTableSplitter:
    """Produces (text, html) pairs for a `<table>` HtmlElement.

    Each chunk contains a whole number of rows whenever possible. An oversized row is split on an
    even cell boundary and a single cell that is by itself too big to fit in the chunking window
    is divided by text-splitting.

    The returned `html` value is always a parseable HTML `<table>` subtree.
    """

    def __init__(self, table_element: HtmlTable, opts: ChunkingOptions):
        self._table_element = table_element
        self._opts = opts

    @classmethod
    def iter_subtables(
        cls, table_element: HtmlTable, opts: ChunkingOptions
    ) -> Iterator[TextAndHtml]:
        """Generate (text, html) pair for each split of this table pre-chunk.

        Each split is on an even row boundary whenever possible, falling back to even cell and even
        word boundaries when a row or cell is by itself oversized, respectively.
        """
        return cls(table_element, opts)._iter_subtables()

    def _iter_subtables(self) -> Iterator[TextAndHtml]:
        """Generate (text, html) pairs containing as many whole rows as will fit in window.

        Falls back to splitting rows into whole cells when a single row is by itself too big to
        fit in the chunking window.
        """
        accum = _RowAccumulator(maxlen=self._opts.hard_max)

        for row in self._table_element.iter_rows():
            # -- if row won't fit, any WIP chunk is done, send it on its way --
            if not accum.will_fit(row):
                yield from accum.flush()
            # -- if row fits, add it to accumulator --
            if accum.will_fit(row):
                accum.add_row(row)
            else:  # -- otherwise, single row is bigger than chunking window --
                yield from self._iter_row_splits(row)

        yield from accum.flush()

    def _iter_row_splits(self, row: HtmlRow) -> Iterator[TextAndHtml]:
        """Split oversized row into (text, html) pairs containing as many cells as will fit."""
        accum = _CellAccumulator(maxlen=self._opts.hard_max)

        for cell in row.iter_cells():
            # -- if cell won't fit, flush and check again --
            if not accum.will_fit(cell):
                yield from accum.flush()
            # -- if cell fits, add it to accumulator --
            if accum.will_fit(cell):
                accum.add_cell(cell)
            else:  # -- otherwise, single cell is bigger than chunking window --
                yield from self._iter_cell_splits(cell)

        yield from accum.flush()

    def _iter_cell_splits(self, cell: HtmlCell) -> Iterator[TextAndHtml]:
        """Split a single oversized cell into sub-sub-sub-table HTML fragments."""
        # -- 33 is len("<table><tr><td></td></tr></table>"), HTML overhead beyond text content --
        opts = ChunkingOptions(max_characters=(self._opts.hard_max - 33))
        split = _TextSplitter(opts)

        text, remainder = split(cell.text)
        yield text, f"<table><tr><td>{text}</td></tr></table>"

        # -- an oversized cell will have a remainder, split that up into additional chunks.
        while remainder:
            text, remainder = split(remainder)
            yield text, f"<table><tr><td>{text}</td></tr></table>"


class _TextSplitter:
    """Provides a text-splitting function configured on construction.

    Text is split on the best-available separator, falling-back from the preferred separator
    through a sequence of alternate separators.

    - The separator is removed by splitting so only whitespace strings are suitable separators.
    - A "blank-line" ("\n\n") is unlikely to occur in an element as it would have been used as an
      element boundary during partitioning.

    This is a *callable* object. Constructing it essentially produces a function:

        split = _TextSplitter(opts)
        fragment, remainder = split(s)

    This allows it to be configured with length-options etc. on construction and used throughout a
    chunking operation on a given element-stream.
    """

    def __init__(self, opts: ChunkingOptions):
        self._opts = opts

    def __call__(self, s: str) -> tuple[str, str]:
        """Return pair of strings split from `s` on the best match of configured patterns.

        The first string is the split, the second is the remainder of the string. The split string
        will never be longer than `maxlen`. The separators are tried in order until a match is
        found. The last separator is "" which matches between any two characters so there will
        always be a split.

        The separator is removed and does not appear in the split or remainder.

        An `s` that is already less than the maximum length is returned unchanged with no remainder.
        This allows this function to be called repeatedly with the remainder until it is consumed
        and returns a remainder of "".
        """
        maxlen = self._opts.hard_max

        if len(s) <= maxlen:
            return s, ""

        for p, sep_len in self._patterns:
            # -- length of separator must be added to include that separator when it happens to be
            # -- located exactly at maxlen. Otherwise the search-from-end regex won't find it.
            fragment, remainder = self._split_from_maxlen(p, sep_len, s)
            if (
                # -- no available split with this separator --
                not fragment
                # -- split did not progress, consuming part of the string --
                or len(remainder) >= len(s)
            ):
                continue
            return fragment.rstrip(), remainder.lstrip()

        # -- the terminal "" pattern is not actually executed via regex since its implementation is
        # -- trivial and provides a hard back-stop here in this method. No separator is used between
        # -- tail and remainder on arb-char split.
        return s[:maxlen].rstrip(), s[maxlen - self._opts.overlap :].lstrip()

    @lazyproperty
    def _patterns(self) -> tuple[tuple[regex.Pattern[str], int], ...]:
        """Sequence of (pattern, len) pairs to match against.

        Patterns appear in order of preference, those following are "fall-back" patterns to be used
        if no match of a prior pattern is found.

        NOTE these regexes search *from the end of the string*, which is what the "(?r)" bit
        specifies. This is much more efficient than starting at the beginning of the string which
        could result in hundreds of matches before the desired one.
        """
        separators = self._opts.text_splitting_separators
        return tuple((regex.compile(f"(?r){sep}"), len(sep)) for sep in separators)

    def _split_from_maxlen(
        self, pattern: regex.Pattern[str], sep_len: int, s: str
    ) -> tuple[str, str]:
        """Return (split, remainder) pair split from `s` on the right-most match before `maxlen`.

        Returns `"", s` if no suitable match was found. Also returns `"", s` if splitting on this
        separator produces a split shorter than the required overlap (which would produce an
        infinite loop).

        `split` will never be longer than `maxlen` and there is no longer split available using
        `pattern`.

        The separator is removed and does not appear in either the split or remainder.
        """
        maxlen, overlap = self._opts.hard_max, self._opts.overlap

        # -- A split not longer than overlap will not progress (infinite loop). On the right side,
        # -- need to extend search range to include a separator located exactly at maxlen.
        match = pattern.search(s, pos=overlap + 1, endpos=maxlen + sep_len)
        if match is None:
            return "", s

        # -- characterize match location
        match_start, match_end = match.span()
        # -- matched separator is replaced by single-space in overlap string --
        separator = " "

        # -- in multi-space situation, fragment may have trailing whitespace because match is from
        # -- right to left
        fragment = s[:match_start].rstrip()
        # -- remainder can have leading space when match is on "\n" followed by spaces --
        raw_remainder = s[match_end:].lstrip()

        if overlap <= len(separator):
            return fragment, raw_remainder

        # -- compute overlap --
        tail_len = overlap - len(separator)
        tail = fragment[-tail_len:].lstrip()
        overlapped_remainder = tail + separator + raw_remainder
        return fragment, overlapped_remainder


class _CellAccumulator:
    """Incrementally build `<table>` fragment cell-by-cell to maximally fill chunking window.

    Accumulate cells until chunking window is filled, then generate the text and HTML for the
    subtable composed of all those rows that fit in the window.
    """

    def __init__(self, maxlen: int):
        self._maxlen = maxlen
        self._cells: list[HtmlCell] = []

    def add_cell(self, cell: HtmlCell) -> None:
        """Add `cell` to this accumulation. Caller is responsible for ensuring it will fit."""
        self._cells.append(cell)

    def flush(self) -> Iterator[TextAndHtml]:
        """Generate zero-or-one (text, html) pairs for accumulated sub-sub-table."""
        if not self._cells:
            return
        text = " ".join(self._iter_cell_texts())
        tds_str = "".join(c.html for c in self._cells)
        html = f"<table><tr>{tds_str}</tr></table>"
        self._cells.clear()
        yield text, html

    def will_fit(self, cell: HtmlCell) -> bool:
        """True when `cell` will fit within remaining space left by accummulated cells."""
        return self._remaining_space >= len(cell.text)

    def _iter_cell_texts(self) -> Iterator[str]:
        """Generate contents of each accumulated cell as a separate string.

        A cell that is empty or contains only whitespace does not generate a string.
        """
        for cell in self._cells:
            if not (text := cell.text):
                continue
            yield text

    @property
    def _remaining_space(self) -> int:
        """Number of characters remaining when text of accumulated cells is joined."""
        # -- separators are one space (" ") at the end of each cell's text, including last one to
        # -- account for space before prospective next cell.
        separators_len = len(self._cells)
        return self._maxlen - separators_len - sum(len(c.text) for c in self._cells)


class _RowAccumulator:
    """Maybe `SubtableAccumulator`.

    Accumulate rows until chunking window is filled, then generate the text and HTML for the
    subtable composed of all those rows that fit in the window.
    """

    def __init__(self, maxlen: int):
        self._maxlen = maxlen
        self._rows: list[HtmlRow] = []

    def add_row(self, row: HtmlRow) -> None:
        """Add `row` to this accumulation. Caller is responsible for ensuring it will fit."""
        self._rows.append(row)

    def flush(self) -> Iterator[TextAndHtml]:
        """Generate zero-or-one (text, html) pairs for accumulated sub-table."""
        if not self._rows:
            return
        text = " ".join(self._iter_cell_texts())
        trs_str = "".join(r.html for r in self._rows)
        html = f"<table>{trs_str}</table>"
        self._rows.clear()
        yield text, html

    def will_fit(self, row: HtmlRow) -> bool:
        """True when `row` will fit within remaining space left by accummulated rows."""
        return self._remaining_space >= row.text_len

    def _iter_cell_texts(self) -> Iterator[str]:
        """Generate contents of each row cell as a separate string.

        A cell that is empty or contains only whitespace does not generate a string.
        """
        for r in self._rows:
            yield from r.iter_cell_texts()

    @property
    def _remaining_space(self) -> int:
        """Number of characters remaining when accumulated rows are formed into HTML."""
        # -- separators are one space (" ") at the end of each row's text, including last one to
        # -- account for space before prospective next row.
        separators_len = len(self._rows)
        return self._maxlen - separators_len - sum(r.text_len for r in self._rows)


# ================================================================================================
# PRE-CHUNK COMBINER
# ================================================================================================


class PreChunkCombiner:
    """Filters pre-chunk stream to combine small pre-chunks where possible."""

    def __init__(self, pre_chunks: Iterable[PreChunk], opts: ChunkingOptions):
        self._pre_chunks = pre_chunks
        self._opts = opts

    def iter_combined_pre_chunks(self) -> Iterator[PreChunk]:
        """Generate pre-chunk objects, combining `PreChunk` objects when they'll fit in window."""
        accum = _PreChunkAccumulator(self._opts)

        for pre_chunk in self._pre_chunks:
            # -- finish accumulating pre-chunk when it's full --
            if not accum.will_fit(pre_chunk):
                yield from accum.flush()

            accum.add_pre_chunk(pre_chunk)

        yield from accum.flush()


class _PreChunkAccumulator:
    """Accumulates, measures, and combines pre-chunks.

    Used for combining pre-chunks for chunking strategies like "by-title" that can potentially
    produce undersized chunks and offer the `combine_text_under_n_chars` option.

    Provides `.add_pre_chunk()` allowing a pre-chunk to be added to the chunk and provides
    monitoring properties `.remaining_space` and `.text_length` suitable for deciding whether to add
    another pre-chunk.

    `.flush()` is used to combine the accumulated pre-chunks into a single `PreChunk` object.
    This method returns an interator that generates zero-or-one `PreChunk` objects and is used
    like so:

        yield from accum.flush()

    If no pre-chunks have been accumulated, no `PreChunk` is generated. Flushing the builder
    clears the pre-chunks it contains so it is ready to accept the next pre-chunk.
    """

    def __init__(self, opts: ChunkingOptions) -> None:
        self._opts = opts
        self._pre_chunk: PreChunk | None = None

    def add_pre_chunk(self, pre_chunk: PreChunk) -> None:
        """Add a pre-chunk to the accumulator for possible combination with next pre-chunk."""
        self._pre_chunk = (
            pre_chunk if self._pre_chunk is None else self._pre_chunk.combine(pre_chunk)
        )

    def flush(self) -> Iterator[PreChunk]:
        """Generate accumulated pre-chunk as a single combined pre-chunk.

        Does not generate a pre-chunk when none has been accumulated.
        """
        # -- nothing to do if no pre-chunk has been accumulated --
        if not self._pre_chunk:
            return
        # -- otherwise generate the combined pre-chunk --
        yield self._pre_chunk
        # -- and reset the accumulator (to empty) --
        self._pre_chunk = None

    def will_fit(self, pre_chunk: PreChunk) -> bool:
        """True when there is room for `pre_chunk` in accumulator.

        An empty accumulator always has room. Otherwise there is only room when `pre_chunk` can be
        combined with any other pre-chunks in the accumulator without exceeding the combination
        limits specified for the chunking run.
        """
        # -- an empty accumulator always has room --
        if self._pre_chunk is None:
            return True

        return self._pre_chunk.can_combine(pre_chunk)


# ================================================================================================
# CHUNK BOUNDARY PREDICATES
# ------------------------------------------------------------------------------------------------
# A *boundary predicate* is a function that takes an element and returns True when the element
# represents the start of a new semantic boundary (such as section or page) to be respected in
# chunking.
#
# Some of the functions below *are* a boundary predicate and others *construct* a boundary
# predicate.
#
# These can be mixed and matched to produce different chunking behaviors like "by_title" or left
# out altogether to produce "basic-chunking" behavior.
#
# The effective lifetime of the function that produce a predicate (rather than directly being one)
# is limited to a single element-stream because these retain state (e.g. current page number) to
# determine when a semantic boundary has been crossed.
# ================================================================================================


def is_on_next_page() -> BoundaryPredicate:
    """Not a predicate itself, calling this returns a predicate that triggers on each new page.

    The lifetime of the returned callable cannot extend beyond a single element-stream because it
    stores current state (current page-number) that is particular to that element stream.

    The returned predicate tracks the "current" page-number, starting at 1. An element with a
    greater page number returns True, indicating the element starts a new page boundary, and
    updates the enclosed page-number ready for the next transition.

    An element with `page_number == None` or a page-number lower than the stored value is ignored
    and returns False.
    """
    current_page_number: int = 1
    is_first: bool = True

    def page_number_incremented(element: Element) -> bool:
        nonlocal current_page_number, is_first

        page_number = element.metadata.page_number

        # -- The first element never reports a page break, it starts the first page of the
        # -- document. That page could be numbered (page_number is non-None) or not. If it is not
        # -- numbered we assign it page-number 1.
        if is_first:
            current_page_number = page_number or 1
            is_first = False
            return False

        # -- An element with a `None` page-number is assumed to continue the current page. It never
        # -- updates the current-page-number because once set, the current-page-number is "sticky"
        # -- until replaced by a different explicit page-number.
        if page_number is None:
            return False

        if page_number == current_page_number:
            return False

        # -- it's possible for a page-number to decrease. We don't expect that, but if it happens
        # -- we consider it a page-break.
        current_page_number = page_number
        return True

    return page_number_incremented


def is_title(element: Element) -> bool:
    """True when `element` is a `Title` element, False otherwise."""
    return isinstance(element, Title)
