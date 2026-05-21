-- dedup-ids.lua — pandoc Lua filter that disambiguates duplicate element
-- IDs in the AST.  Pandoc's own auto_identifiers logic disambiguates
-- duplicates within the parsed document but does NOT cover IDs that the
-- EPUB writer synthesises later (e.g. the bodymatter "preamble" chunk
-- generated from `--metadata title` when the markdown has content before
-- its first H1).  Without this filter pandoc happily emits two
-- `<section id="aws-well-architected-framework">` elements in two
-- different XHTML files and our audit (rightly) reports a duplicate id.
--
-- Filter is idempotent.  Run via `--lua-filter share/pandoc/dedup-ids.lua`.

local seen = {}

local function dedup(el)
    if el.identifier ~= nil and el.identifier ~= "" then
        local id = el.identifier
        if seen[id] then
            seen[id] = seen[id] + 1
            el.identifier = id .. "-" .. (seen[id] - 1)
        else
            seen[id] = 1
        end
    end
    return el
end

function Header(el)
    return dedup(el)
end

function Div(el)
    return dedup(el)
end

function Span(el)
    return dedup(el)
end

function CodeBlock(el)
    return dedup(el)
end

function Code(el)
    return dedup(el)
end

function Link(el)
    -- Links themselves can carry identifiers via attr_list `[text](url){#x}`
    return dedup(el)
end

function Image(el)
    return dedup(el)
end

function Table(el)
    return dedup(el)
end
