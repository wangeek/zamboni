ALTER TABLE
 `reviews`
ADD COLUMN
 `deleted` BOOLEAN DEFAULT FALSE
DROP COLUMN
 `sandbox`;
