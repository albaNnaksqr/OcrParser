from .config import ParserConfig

__all__ = ["ParserConfig", "DotsOCRParser", "DotsOCRParserOptimized"]


def __getattr__(name):
    if name in {"DotsOCRParser", "DotsOCRParserOptimized"}:
        from .parser import DotsOCRParser, DotsOCRParserOptimized

        return {
            "DotsOCRParser": DotsOCRParser,
            "DotsOCRParserOptimized": DotsOCRParserOptimized,
        }[name]
    raise AttributeError(name)
