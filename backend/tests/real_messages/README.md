# Real Outlook messages, for testing against

Drop genuine `.msg` or `.eml` files here and the suite will run the whole
intake path over each one. With none present those tests skip, so a fresh
checkout still passes.

**Nothing in this folder is committed.** These are real customer emails —
their drawings, their addresses, their prices. `.gitignore` excludes
everything here except this note, and it must stay that way: this repository
is public.

What the tests check, per file:

* it parses at all, and yields a sender, a subject and a body
* every attachment arrives with its actual bytes, not just a name
* drawings are told apart from signature logos and footers
* each drawing rasterises to an image the vision model can be given

That last one is the point. A `.msg` that parses but whose drawings will not
render is a file that reaches the estimator looking fine and reads as blank.
