def uri_to_zip_entry_name(base, uri):
    if uri.startswith("/"):
        return uri[1:]
    else:
        return base + "/" + uri


def resolve_relative_uri(base, uri):
    if uri.startswith("/"):
        path = uri[1:]
    elif base:
        path = base + "/" + uri
    else:
        path = uri

    segments = []
    for segment in path.split("/"):
        if segment == "..":
            if segments and segments[-1] != "..":
                segments.pop()
            else:
                segments.append(segment)
        elif segment == ".":
            continue
        else:
            segments.append(segment)

    return "/".join(segments)


def replace_fragment(uri, fragment):
    hash_index = uri.find("#")
    if hash_index != -1:
        uri = uri[:hash_index]
    return uri + "#" + fragment
