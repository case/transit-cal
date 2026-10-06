// Compiled in the image build against the jars in ics-validate.lock.

import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import net.fortuna.ical4j.data.CalendarBuilder;
import net.fortuna.ical4j.data.ParserException;
import net.fortuna.ical4j.validate.ValidationEntry;
import net.fortuna.ical4j.validate.ValidationException;
import net.fortuna.ical4j.validate.ValidationResult;

/** Validate .ics files against RFC 5545 with ical4j. Exits 1 if any file has a validation entry. */
public class IcsValidate {
    public static void main(String[] args) {
        if (args.length == 0) {
            System.err.println("usage: IcsValidate FILE.ics...");
            System.exit(2);
        }
        int failed = 0;
        for (String arg : args) {
            if (!validate(Path.of(arg))) {
                failed++;
            }
        }
        System.out.printf("%d of %d files valid%n", args.length - failed, args.length);
        System.exit(failed == 0 ? 0 : 1);
    }

    private static boolean validate(Path path) {
        try (Reader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            ValidationResult result = new CalendarBuilder().build(reader).validate(true);
            boolean clean = result.getEntries().isEmpty();
            System.out.printf("%s: %s%n", path, clean ? "valid" : "INVALID");
            for (ValidationEntry entry : result.getEntries()) {
                System.out.printf(
                        "  %s %s: %s%n", entry.getSeverity(), entry.getContext(), entry.getMessage());
            }
            return clean;
        } catch (IOException | ParserException | ValidationException e) {
            System.out.printf("%s: INVALID (%s)%n", path, e.getMessage());
            return false;
        }
    }
}
